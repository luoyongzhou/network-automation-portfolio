#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vSphere IP验证系统 v6.4 (阶段2激进优化版)
功能：
1. vSphere虚机状态同步
2. 全网段Ping扫描（100并发）
3. 端口扫描（批次超时保护）
"""
import os, sys, json, time, random, socket, subprocess, traceback, re, csv
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, TimeoutError as FutureTimeoutError
try:
    import requests
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    import pynetbox
    from openpyxl import Workbook
    from loguru import logger
    from dotenv import load_dotenv
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
    import ssl, atexit, ipaddress
except ImportError as e:
    print(f"缺少依赖: {e}")
    sys.exit(1)

# 基础路径配置
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "vsphere_validator"
LOG_DIR = BASE_DIR / "logs" / "vsphere_validator"

# 创建必要目录
for d in [DATA_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)
for s in ['vsphere', 'ping', 'portscan', 'reports', 'state']:
    (DATA_DIR / s).mkdir(exist_ok=True)

# 配置日志
logger.remove()
logger.add(LOG_DIR / "validator.log", rotation="10 MB", retention="2 days", level="DEBUG", encoding="utf-8")
logger.add(sys.stdout, level="INFO")

# 加载环境变量
env_file = BASE_DIR / ".env"
if env_file.exists():
    load_dotenv(env_file)

# vSphere配置
VC = {
    'host': os.getenv('VSPHERE_HOST'),
    'user': os.getenv('VSPHERE_USER'),
    'password': os.getenv('VSPHERE_PASSWORD'),
    'port': 443
}

# NetBox配置
NC = {
    'url': os.getenv('NETBOX_URL', 'http://localhost:8001'),
    'token': os.getenv('NETBOX_TOKEN')
}

# 阶段2 Ping扫描配置（激进优化）
# 以下网段均为示例，请根据实际环境在 .env 中配置或自行修改
PING_EXCLUDE = os.getenv('PING_EXCLUDE', '172.16.0.0/12,192.168.0.0/16').split(',')  # 排除网段
SCAN_TARGETS = os.getenv('SCAN_TARGETS', '10.0.0.0/24,10.0.1.0/24').split(',')       # 端口扫描目标网段
PING_WORKERS = 100      # 100并发（激进模式）
PING_BATCH = 1000       # 1000个IP/批
BATCH_PAUSE = 3         # 批次间隔3秒

# 阶段3端口扫描配置（批次处理 + 超时保护）
PORT_SCAN_CONFIG = {
    'workers': 5,                    # 5个IP并发
    'batch_size': 20,                # 每批次20个IP
    'batch_timeout': 300,            # 单批次超时5分钟
    'ports': [22, 80, 443, 3389, 3306, 5432, 8080, 8443],  # 扫描端口
    'port_delay': (0.3, 0.7),        # 端口间延迟0.3-0.7秒
    'ip_delay': (3, 7),              # IP间延迟3-7秒
    'timeout': 0.5,                  # 单端口超时0.5秒
    'single_ip_timeout': 20,         # 单个IP总超时20秒
    'task_timeout': 30,              # 单个任务超时30秒
}

def parse_time(s):
    """解析时间字符串"""
    try:
        return datetime.strptime(s.split('.')[0].replace('T', ' '), '%Y-%m-%d %H:%M:%S')
    except:
        return datetime.now()

def is_excluded(ip):
    """检查IP是否在排除列表中"""
    try:
        ip_obj = ipaddress.ip_address(ip)
        for ex_net in PING_EXCLUDE:
            if ip_obj in ipaddress.ip_network(ex_net):
                return True
    except:
        pass
    return False

def is_subnet_of(subnet, supernet):
    """检查subnet是否是supernet的子网"""
    try:
        sub = ipaddress.ip_network(subnet)
        sup = ipaddress.ip_network(supernet)
        return (sub.network_address >= sup.network_address and
                sub.broadcast_address <= sup.broadcast_address)
    except:
        return False

class VSphereHelper:
    """vSphere连接和操作类"""
    def __init__(self, cfg):
        self.cfg = cfg
        self.si = None
        self.content = None
    
    def connect(self):
        """连接vSphere"""
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            logger.info(f"正在连接vSphere: {self.cfg['host']}...")
            self.si = SmartConnect(
                host=self.cfg['host'],
                user=self.cfg['user'],
                pwd=self.cfg['password'],
                port=self.cfg['port'],
                sslContext=ctx
            )
            atexit.register(Disconnect, self.si)
            self.content = self.si.RetrieveContent()
            logger.success("✓ vSphere连接成功")
            return True
        except Exception as e:
            logger.error(f"✗ vSphere连接失败: {e}")
            return False
    
    def get_vms(self):
        """获取所有虚拟机列表"""
        vms = []
        try:
            logger.info("正在获取虚拟机列表...")
            container = self.content.viewManager.CreateContainerView(
                self.content.rootFolder,
                [vim.VirtualMachine],
                True
            )
            for vm in container.view:
                try:
                    # 跳过模板
                    if not (vm.config and vm.config.template):
                        vms.append({
                            'name': vm.name,
                            'state': vm.runtime.powerState
                        })
                except:
                    pass
            container.Destroy()
            logger.success(f"✓ 获取到 {len(vms)} 台虚拟机")
        except Exception as e:
            logger.error(f"✗ 获取虚拟机失败: {e}")
        return vms
    
    @staticmethod
    def extract_ip(name):
        """从虚机名称提取IP（支持分隔符：- _ 空格 点号）"""
        matches = re.findall(
            r'(?:^|[-_\s.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?:[-_\s.]|$)',
            name
        )
        for ip in matches:
            try:
                parts = ip.split('.')
                if all(0 <= int(p) <= 255 for p in parts):
                    first = int(parts[0])
                    # 优先返回内网IP
                    if first == 10 or \
                       (first == 172 and 16 <= int(parts[1]) <= 31) or \
                       (first == 192 and int(parts[1]) == 168):
                        return ip
                    # 其次返回公网IP（排除127）
                    if 1 <= first <= 223 and first != 127:
                        return ip
            except:
                pass
        return None
    
    @staticmethod
    def has_date(name):
        """检查虚机名称是否包含日期-202x"""
        return bool(re.search(r'-202\d', name))

class NetBoxHelper:
    """NetBox API操作类"""
    def __init__(self, cfg):
        self.nb = pynetbox.api(cfg['url'], token=cfg['token'])
        self.nb.http_session.verify = False
        self.prefix_cache = {}  # 网段缓存
        logger.success("✓ NetBox连接成功")
    
    def find_prefix(self, ip):
        """查找IP所属网段"""
        try:
            ip_obj = ipaddress.ip_address(ip)
            # 先查缓存
            for ps, po in self.prefix_cache.items():
                if ip_obj in ipaddress.ip_network(ps):
                    return po
            # 查API
            for p in self.nb.ipam.prefixes.all():
                ps = str(p.prefix)
                self.prefix_cache[ps] = p
                if ip_obj in ipaddress.ip_network(ps):
                    return p
        except:
            pass
        return None
    
    def exists(self, ip):
        """检查IP是否存在"""
        try:
            return len(list(self.nb.ipam.ip_addresses.filter(address=ip))) > 0
        except:
            return False
    
    def get_desc(self, ip):
        """获取IP描述"""
        try:
            ips = list(self.nb.ipam.ip_addresses.filter(address=ip))
            return ips[0].description or "" if ips else ""
        except:
            return ""
    
    def create(self, ip, st, desc):
        """创建IP地址"""
        try:
            pf = self.find_prefix(ip)
            if not pf:
                logger.debug(f"    {ip}: 未找到所属网段")
                return False
            self.nb.ipam.ip_addresses.create(
                address=f"{ip}/32",
                status=st,
                description=desc
            )
            time.sleep(0.05)  # API限流
            return True
        except Exception as e:
            logger.error(f"    {ip}: 创建失败 - {e}")
            return False
    
    def update(self, ip, st, desc):
        """更新IP状态和描述"""
        try:
            ips = list(self.nb.ipam.ip_addresses.filter(address=ip))
            if ips:
                ips[0].update({'status': st, 'description': desc})
                time.sleep(0.05)
                return True
        except:
            pass
        return False
    
    def update_desc(self, ip, desc):
        """仅更新IP描述"""
        try:
            ips = list(self.nb.ipam.ip_addresses.filter(address=ip))
            if ips:
                ips[0].update({'description': desc})
                time.sleep(0.05)
                return True
        except:
            pass
        return False
    
    def get_prefixes(self):
        """获取所有网段"""
        try:
            ps = list(self.nb.ipam.prefixes.all())
            logger.info(f"获取到 {len(ps)} 个网段")
            return ps
        except:
            return []

class Scanner:
    """网络扫描工具类"""
    @staticmethod
    def ping(ip):
        """Ping扫描"""
        try:
            r = subprocess.run(
                ['ping', '-c', '1', '-W', '1', ip],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2
            )
            return r.returncode == 0
        except:
            return False
    
    @staticmethod
    def tcp(ip, port, timeout=0.5):
        """TCP端口扫描"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            r = sock.connect_ex((ip, port))
            sock.close()
            return r == 0
        except Exception as e:
            logger.debug(f"    TCP扫描异常 {ip}:{port} - {e}")
            return False

class Validator:
    """主验证类"""
    def __init__(self):
        self.vs = VSphereHelper(VC)
        self.nb = NetBoxHelper(NC)
        self.sc = Scanner()
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.stats = {}
    
    def run(self):
        """主执行流程"""
        logger.info("="*80)
        logger.info("vSphere IP验证系统 v6.4 (阶段2激进优化版)")
        logger.info("="*80)
        logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*80)
        st = time.time()
        
        try:
            # 阶段1：vSphere虚机状态同步
            logger.info("\n" + "="*80)
            logger.info("[阶段1/3] vSphere虚机状态同步")
            logger.info("="*80)
            stage1_start = time.time()
            if not self.stage1():
                logger.error("✗ 阶段1失败，终止执行")
                return False
            self.flag(1)
            logger.success(f"✓ 阶段1完成，耗时 {self.format_time(time.time() - stage1_start)}")
            logger.info(f"  数据保存: data/vsphere_validator/vsphere/{self.today}.csv")
            logger.info(f"  标志文件: data/vsphere_validator/state/s1.flag")
            
            # 阶段2：全网段Ping扫描
            logger.info("\n" + "="*80)
            logger.info("[阶段2/3] 全网段Ping扫描 (激进优化)")
            logger.info("="*80)
            logger.info(f"配置: {PING_WORKERS}个并发, {PING_BATCH}个IP/批, {BATCH_PAUSE}秒/批次间隔")
            logger.warning(f"⚠ 激进模式：100并发可能触发网络设备告警，建议非工作时段执行")
            stage2_start = time.time()
            if not self.check(1):
                logger.error("✗ 阶段1未完成，跳过阶段2")
                return False
            if not self.stage2():
                logger.error("✗ 阶段2失败")
                return False
            self.flag(2)
            logger.success(f"✓ 阶段2完成，耗时 {self.format_time(time.time() - stage2_start)}")
            logger.info(f"  数据保存: data/vsphere_validator/state/unreachable_ips.json")
            logger.info(f"  标志文件: data/vsphere_validator/state/s2.flag")
            
            # 阶段3：端口扫描
            logger.info("\n" + "="*80)
            logger.info("[阶段3/3] 端口扫描 (批次处理 + 超时保护)")
            logger.info("="*80)
            logger.info(f"配置: {PORT_SCAN_CONFIG['workers']}并发, {PORT_SCAN_CONFIG['batch_size']}个IP/批, {PORT_SCAN_CONFIG['batch_timeout']}秒/批次超时")
            stage3_start = time.time()
            if self.check(2):
                self.stage3()
                self.flag(3)
                logger.success(f"✓ 阶段3完成，耗时 {self.format_time(time.time() - stage3_start)}")
                logger.info(f"  标志文件: data/vsphere_validator/state/s3.flag")
            
            total_time = time.time() - st
            logger.info("\n" + "="*80)
            logger.success(f"✓ 全部流程完成！")
            logger.info("="*80)
            logger.info(f"总耗时: {self.format_time(total_time)}")
            logger.info(f"执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"日志文件: logs/vsphere_validator/validator.log")
            logger.info("="*80)
            return True
            
        except KeyboardInterrupt:
            logger.warning("\n✗ 用户中断执行")
            return False
        except Exception as e:
            logger.error(f"\n✗ 执行失败: {e}")
            traceback.print_exc()
            return False
    
    def format_time(self, seconds):
        """格式化时间显示"""
        if seconds < 60:
            return f"{seconds:.1f}秒"
        elif seconds < 3600:
            return f"{seconds/60:.1f}分钟"
        else:
            return f"{seconds/3600:.2f}小时"
    
    def stage1(self):
        """阶段1：vSphere虚机状态同步"""
        if not self.vs.connect():
            return False
        
        vms = self.vs.get_vms()
        if not vms:
            return False
        
        logger.info("正在处理虚拟机IP...")
        
        # 按IP分组（处理重复IP）
        ip_groups = {}
        for vm in vms:
            ip = self.vs.extract_ip(vm['name'])
            if not ip:
                continue
            
            # 判定状态
            if vm['state'] == 'poweredOn':
                status = 'active'
                power_desc = '运行中'
            else:
                power_desc = '已关机'
                if self.vs.has_date(vm['name']):
                    status = 'deprecated'  # 关机+带日期
                else:
                    status = 'reserved'    # 关机+不带日期
            
            if ip not in ip_groups:
                ip_groups[ip] = []
            
            ip_groups[ip].append({
                'vm_name': vm['name'],
                'status': status,
                'power_desc': power_desc
            })
        
        res = []
        stats = {
            'total': len(vms),
            'ips': 0,
            'updated': 0,
            'created': 0,
            'active': 0,
            'reserved': 0,
            'deprecated': 0,
            'duplicates': 0
        }
        
        # 状态优先级：active > reserved > deprecated
        status_priority = {'active': 0, 'reserved': 1, 'deprecated': 2}
        
        for ip, vm_list in ip_groups.items():
            stats['ips'] += 1
            
            # 检测重复
            is_duplicate = len(vm_list) > 1
            if is_duplicate:
                stats['duplicates'] += 1
            
            # 按优先级排序，选择最高优先级
            vm_list_sorted = sorted(vm_list, key=lambda x: status_priority[x['status']])
            selected = vm_list_sorted[0]
            final_status = selected['status']
            stats[final_status] += 1
            
            # 构建描述
            if is_duplicate:
                desc = f"虚机: {selected['vm_name']} ({selected['power_desc']}) | ⚠ 重复"
            else:
                desc = f"虚机: {selected['vm_name']} ({selected['power_desc']})"
            
            # 更新或创建NetBox记录
            if self.nb.exists(ip):
                self.nb.update(ip, final_status, desc)
                stats['updated'] += 1
            else:
                self.nb.create(ip, final_status, desc)
                stats['created'] += 1
            
            res.append({'IP': ip, 'VM': selected['vm_name'], '状态': final_status})
        
        # 保存CSV
        self.save_csv(res, DATA_DIR / "vsphere" / f"{self.today}.csv")
        self.stats['s1'] = stats
        
        # 输出统计
        logger.success(f"提取IP: {stats['ips']}个")
        logger.info(f"  - 更新: {stats['updated']}个")
        logger.info(f"  - 创建: {stats['created']}个")
        logger.info(f"  - active: {stats['active']}个")
        logger.info(f"  - reserved: {stats['reserved']}个")
        logger.info(f"  - deprecated: {stats['deprecated']}个")
        if stats['duplicates'] > 0:
            logger.warning(f"  - ⚠ 重复IP: {stats['duplicates']}个")
        
        return True
    
    def stage2(self):
        """阶段2：全网段Ping扫描"""
        pfs = self.nb.get_prefixes()
        if not pfs:
            return False
        
        logger.info("正在生成待扫描IP列表...")
        ips = []
        ex = 0
        
        # 遍历所有网段生成IP列表
        for p in pfs:
            try:
                prefix_str = str(p.prefix)
                net = ipaddress.ip_network(prefix_str)
                
                # 检查是否在排除列表
                skip = any(is_subnet_of(prefix_str, e) for e in PING_EXCLUDE)
                if skip:
                    logger.info(f"  跳过: {prefix_str}")
                    ex += net.num_addresses - 2
                    continue
                
                # 生成主机IP
                for ip in net.hosts():
                    ip_str = str(ip)
                    if is_excluded(ip_str):
                        ex += 1
                    else:
                        ips.append(ip_str)
            except Exception as e:
                logger.warning(f"处理网段失败 {p.prefix}: {e}")
        
        logger.info(f"待扫描: {len(ips)}个IP (已排除 {ex}个)")
        
        if not ips:
            logger.warning("没有待扫描的IP")
            return True
        
        # 随机打乱IP顺序
        random.shuffle(ips)
        
        unreach = []
        stats = {'scanned': 0, 'reach': 0, 'unreach': 0, 'updated': 0, 'created': 0}
        total_batches = (len(ips) + PING_BATCH - 1) // PING_BATCH
        
        logger.info(f"开始扫描，共 {total_batches} 个批次...")
        
        # 批次扫描
        for batch_idx in range(0, len(ips), PING_BATCH):
            batch = ips[batch_idx:batch_idx+PING_BATCH]
            batch_num = batch_idx // PING_BATCH + 1
            
            logger.info(f"\n>>> 批次 {batch_num}/{total_batches} (扫描{len(batch)}个IP)")
            batch_start = time.time()
            
            # 并发Ping扫描
            ping_results = {}
            with ThreadPoolExecutor(max_workers=PING_WORKERS) as executor:
                futures = {executor.submit(self.sc.ping, ip): ip for ip in batch}
                for future in as_completed(futures):
                    ip = futures[future]
                    try:
                        ping_results[ip] = future.result()
                    except:
                        ping_results[ip] = False
            
            # 处理扫描结果
            for ip in batch:
                ok = ping_results.get(ip, False)
                stats['scanned'] += 1
                stats['reach' if ok else 'unreach'] += 1
                
                if self.nb.exists(ip):
                    # 已存在：追加Ping结果
                    cur = self.nb.get_desc(ip)
                    new = re.sub(r'\s*\|\s*Ping:\s*[^\|]+', '', cur).strip()
                    new = f"{new} | Ping: {'可达' if ok else '不可达'}" if new else f"Ping: {'可达' if ok else '不可达'}"
                    if new != cur:
                        self.nb.update_desc(ip, new)
                        stats['updated'] += 1
                else:
                    # 不存在
                    if ok:
                        # 可达：创建
                        self.nb.create(ip, 'active', '非虚机Ping可达')
                        stats['created'] += 1
                    else:
                        # 不可达：记录到列表
                        unreach.append(ip)
            
            # 批次统计
            batch_time = time.time() - batch_start
            progress = stats['scanned'] / len(ips) * 100
            speed = len(batch) / batch_time if batch_time > 0 else 0
            logger.info(f"    进度: {stats['scanned']}/{len(ips)} ({progress:.1f}%)")
            logger.info(f"    可达: {stats['reach']} | 不可达: {stats['unreach']}")
            logger.info(f"    耗时: {batch_time:.1f}秒 (速度: {speed:.0f} IP/秒)")
            
            # 批次间延迟
            if batch_idx + PING_BATCH < len(ips):
                logger.debug(f"    暂停 {BATCH_PAUSE} 秒...")
                time.sleep(BATCH_PAUSE)
        
        # 保存不可达IP列表
        with open(DATA_DIR / "state" / "unreachable_ips.json", 'w') as f:
            json.dump({'date': self.today, 'ips': unreach}, f)
        
        self.stats['s2'] = stats
        
        # 输出统计
        logger.success(f"\n扫描完成: {stats['scanned']}个")
        logger.info(f"  - 可达: {stats['reach']}个")
        logger.info(f"  - 不可达: {stats['unreach']}个")
        logger.info(f"  - 更新: {stats['updated']}个")
        logger.info(f"  - 创建: {stats['created']}个")
        
        return True
    
    def stage3(self):
        """阶段3：端口扫描（批次处理）"""
        uf = DATA_DIR / "state" / "unreachable_ips.json"
        if not uf.exists():
            logger.warning("未找到待扫描列表")
            return False
        
        with open(uf) as f:
            unreach = json.load(f).get('ips', [])
        
        logger.info(f"加载不可达IP: {len(unreach)}个")
        
        # 筛选目标网段
        targets = []
        for ip in unreach:
            try:
                ip_obj = ipaddress.ip_address(ip)
                if any(ip_obj in ipaddress.ip_network(t) for t in SCAN_TARGETS):
                    targets.append(ip)
            except:
                pass
        
        logger.info(f"目标网段筛选: {len(targets)}个IP")
        logger.info(f"  目标: {', '.join(SCAN_TARGETS)}")
        
        if not targets:
            logger.info("无需扫描")
            return True
        
        random.shuffle(targets)
        
        logger.info(f"开始端口扫描 (全量扫描{len(targets)}个IP)...")
        logger.info(f"  扫描端口: {PORT_SCAN_CONFIG['ports']}")
        logger.info(f"  批次配置: {PORT_SCAN_CONFIG['batch_size']}个IP/批, {PORT_SCAN_CONFIG['batch_timeout']}秒/批次超时")
        
        total_created = 0
        total_scanned = 0
        total_failed = 0
        total_timeout = 0
        
        def scan_single_ip(ip):
            """扫描单个IP的所有端口"""
            logger.debug(f"  >>> 开始扫描 {ip}")
            open_ports = []
            start_time = time.time()
            
            try:
                for port in PORT_SCAN_CONFIG['ports']:
                    # 单IP超时保护
                    elapsed = time.time() - start_time
                    if elapsed > PORT_SCAN_CONFIG['single_ip_timeout']:
                        logger.warning(f"    {ip}: 单IP超时 ({elapsed:.1f}秒)，跳过剩余端口")
                        break
                    
                    port_start = time.time()
                    try:
                        if self.sc.tcp(ip, port, PORT_SCAN_CONFIG['timeout']):
                            open_ports.append(port)
                            logger.debug(f"    {ip}:{port} - 开放 ({time.time()-port_start:.2f}秒)")
                        else:
                            logger.debug(f"    {ip}:{port} - 关闭")
                    except Exception as e:
                        logger.debug(f"    {ip}:{port} - 异常: {e}")
                    
                    # 端口间延迟
                    time.sleep(random.uniform(*PORT_SCAN_CONFIG['port_delay']))
                
                total_time = time.time() - start_time
                logger.debug(f"  <<< 完成扫描 {ip} (耗时{total_time:.1f}秒, 开放端口: {open_ports})")
                return ip, open_ports, None
                
            except Exception as e:
                error_msg = f"扫描异常: {type(e).__name__} - {str(e)}"
                logger.error(f"    {ip}: {error_msg}")
                return ip, [], error_msg
        
        batch_size = PORT_SCAN_CONFIG['batch_size']
        total_batches = (len(targets) + batch_size - 1) // batch_size
        
        logger.info(f"\n开始批次扫描，共 {total_batches} 个批次...")
        
        # 批次扫描
        for batch_idx in range(0, len(targets), batch_size):
            batch = targets[batch_idx:batch_idx+batch_size]
            batch_num = batch_idx // batch_size + 1
            
            logger.info(f"\n>>> 批次 {batch_num}/{total_batches} (扫描{len(batch)}个IP)")
            batch_start = time.time()
            
            created = 0
            scanned = 0
            failed = 0
            timeout_count = 0
            
            with ThreadPoolExecutor(max_workers=PORT_SCAN_CONFIG['workers']) as executor:
                logger.debug(f"提交 {len(batch)} 个扫描任务...")
                futures = {executor.submit(scan_single_ip, ip): ip for ip in batch}
                
                try:
                    # 批次超时控制
                    for future in as_completed(futures, timeout=PORT_SCAN_CONFIG['batch_timeout']):
                        ip = futures[future]
                        scanned += 1
                        total_scanned += 1
                        
                        try:
                            result_ip, open_ports, error = future.result(timeout=PORT_SCAN_CONFIG['task_timeout'])
                            
                            if error:
                                failed += 1
                                total_failed += 1
                                logger.warning(f"  [{scanned}/{len(batch)}] {result_ip}: 失败 - {error}")
                            elif open_ports:
                                # 有开放端口
                                if not self.nb.exists(result_ip):
                                    desc = f"非虚机端口存活 | TCP: {', '.join(map(str, open_ports[:2]))}"
                                    if self.nb.create(result_ip, 'active', desc):
                                        created += 1
                                        total_created += 1
                                        logger.info(f"  [{scanned}/{len(batch)}] {result_ip}: ✓ 创建成功 (端口: {open_ports})")
                                    else:
                                        failed += 1
                                        total_failed += 1
                                        logger.warning(f"  [{scanned}/{len(batch)}] {result_ip}: ✗ 创建失败")
                                else:
                                    logger.debug(f"  [{scanned}/{len(batch)}] {result_ip}: 已存在 (端口: {open_ports})")
                            else:
                                logger.debug(f"  [{scanned}/{len(batch)}] {result_ip}: 无开放端口")
                            
                            # IP间延迟
                            if scanned < len(batch):
                                delay = random.uniform(*PORT_SCAN_CONFIG['ip_delay'])
                                logger.debug(f"  IP间延迟: {delay:.1f}秒")
                                time.sleep(delay)
                            
                        except FutureTimeoutError:
                            timeout_count += 1
                            total_timeout += 1
                            failed += 1
                            total_failed += 1
                            logger.error(f"失败")
                            logger.error(f"  [{scanned}/{len(batch)}] {ip}: ✗ 异常 - {type(e).__name__}: {e}")
                    
                except FutureTimeoutError:
                    logger.error(f"✗ 批次超时 ({PORT_SCAN_CONFIG['batch_timeout']}秒)！已扫描 {scanned}/{len(batch)} 个IP")
                    total_timeout += (len(batch) - scanned)
                    # 取消未完成任务
                    for future in futures:
                        if not future.done():
                            future.cancel()
            
            batch_time = time.time() - batch_start
            logger.info(f"    批次耗时: {batch_time:.1f}秒")
            logger.info(f"    批次结果: 扫描{scanned}/{len(batch)}个, 创建{created}个, 失败{failed}个, 超时{timeout_count}个")
        
        # 总计统计
        logger.info("\n" + "="*60)
        logger.success(f"扫描完成: {total_scanned}/{len(targets)}个IP")
        logger.info(f"  - 创建: {total_created}个")
        logger.info(f"  - 失败: {total_failed}个")
        logger.info(f"  - 超时: {total_timeout}个")
        logger.info("="*60)
        
        return True
    
    def save_csv(self, data, path):
        """保存数据到CSV"""
        if data:
            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.DictWriter(f, fieldnames=data[0].keys())
                w.writeheader()
                w.writerows(data)
    
    def check(self, n):
        """检查阶段n是否在24小时内完成"""
        f = DATA_DIR / "state" / f"s{n}.flag"
        if not f.exists():
            return False
        try:
            d = json.loads(f.read_text())
            t = parse_time(d['time'])
            return (datetime.now() - t).total_seconds() / 3600 <= 24
        except:
            return False
    
    def flag(self, n):
        """标记阶段n完成"""
        with open(DATA_DIR / "state" / f"s{n}.flag", 'w') as f:
            json.dump({
                'stage': n,
                'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }, f)

def main():
    """入口函数"""
    try:
        return 0 if Validator().run() else 1
    except Exception as e:
        logger.error(f"失败: {e}")
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
