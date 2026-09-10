#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import logging
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv

# 加载环境变量（.env 文件与本脚本同目录）
load_dotenv(Path(__file__).parent / ".env")

# ==================== 配置（生产环境）====================
class Config:
    LIBRENMS_URL = os.getenv("LIBRENMS_URL", "http://localhost:8000")
    API_TOKEN = os.getenv("LIBRENMS_API_TOKEN")
    DINGTALK_WEBHOOKS = [
        wh.strip() for wh in os.getenv("DINGTALK_WEBHOOKS", "").split(",") if wh.strip()
    ]
    DB_CONTAINER = os.getenv("DB_CONTAINER", "librenms-db")
    DB_USER = os.getenv("DB_USER", "librenms")
    DB_PASS = os.getenv("DB_PASS")
    DB_NAME = os.getenv("DB_NAME", "librenms")
    
    BASE_DIR = Path(__file__).resolve().parent
    STATE_FILE = BASE_DIR / "data" / "alert_state.json"
    LOG_FILE = BASE_DIR / "logs" / "alert.log"
    
    # 需要详细处理的规则 ID（示例：13 = 链路状态 up→down）
    DETAIL_RULE_IDS = [13]
    
    # 重复提醒间隔（秒）- 10 分钟
    REPEAT_INTERVAL = 600

def check_required_config():
    missing = []
    if not Config.API_TOKEN:
        missing.append("LIBRENMS_API_TOKEN")
    if not Config.DB_PASS:
        missing.append("DB_PASS")
    if not Config.DINGTALK_WEBHOOKS:
        missing.append("DINGTALK_WEBHOOKS")
    if missing:
        print(f"缺少必要的环境变量: {', '.join(missing)}，请参考 .env.example 配置 .env 文件")
        sys.exit(1)

def setup_logging():
    Config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG,
        format='[%(asctime)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.FileHandler(Config.LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

class LibreNMSAPI:
    def __init__(self):
        self.base_url = Config.LIBRENMS_URL
        self.headers = {'X-Auth-Token': Config.API_TOKEN}
    
    def get_alerts(self):
        try:
            response = requests.get(
                f"{self.base_url}/api/v0/alerts",
                headers=self.headers,
                params={'state': 1},
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"获取告警失败: {e}")
            return None

class DatabaseQuery:
    def __init__(self):
        self.container = Config.DB_CONTAINER
        self.user = Config.DB_USER
        self.password = Config.DB_PASS
        self.database = Config.DB_NAME
    
    def execute_sql(self, sql):
        """通过 docker exec 执行 SQL"""
        try:
            cmd = [
                'docker', 'exec', self.container,
                'mysql',
                f'-u{self.user}',
                f'-p{self.password}',
                self.database,
                '-sN',
                '-e', sql
            ]
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True
            )
            stdout, stderr = process.communicate(timeout=10)
            
            if process.returncode != 0:
                logging.error(f"SQL 执行失败: {stderr.strip()}")
                return []
            
            lines = [line.strip() for line in stdout.strip().split('\n') if line.strip()]
            return lines
        
        except Exception as e:
            logging.error(f"数据库查询异常: {e}")
            return []
    
    def get_device_info(self, device_id):
        """
        查询设备名称（sysName）和机房位置（location）
        直接查数据库 devices 表联 locations 表，不依赖 API 是否返回这些字段
        返回: {'sysname': str, 'location': str}，查询失败时对应字段为空字符串
        """
        sql = f"""
            SELECT
                IFNULL(devices.sysName, ''),
                IFNULL(locations.location, '')
            FROM devices
            LEFT JOIN locations ON devices.location_id = locations.id
            WHERE devices.device_id = {device_id}
            LIMIT 1
        """
        result = self.execute_sql(sql)
        if result:
            parts = result[0].split('\t')
            sysname = parts[0] if len(parts) >= 1 else ''
            location = parts[1] if len(parts) >= 2 else ''
            return {'sysname': sysname, 'location': location}
        return {'sysname': '', 'location': ''}

    def get_failed_ports_rule13(self, device_id):
        """
        Rule 13: 链路状态up转down
        返回当前所有故障接口（不依赖 poll_time 作为故障时间）
        """
        sql = f"""
            SELECT 
                ports.ifName,
                ports.ifOperStatus,
                ports.ifOperStatus_prev
            FROM ports
            WHERE ports.device_id = {device_id}
              AND ports.ifOperStatus NOT LIKE '%up%'
              AND ports.ifOperStatus_prev LIKE '%up%'
            LIMIT 20
        """
        
        logging.debug(f"Rule 13 查询 SQL: device_id={device_id}")
        result = self.execute_sql(sql)
        
        # 解析结果
        ports_data = []
        for line in result:
            parts = line.split('\t')
            if len(parts) >= 3:
                ports_data.append({
                    'ifName': parts[0],
                    'operStatus': parts[1],
                    'prevStatus': parts[2]
                })
        
        # Fallback: 查当前所有非 up 的接口
        if not ports_data:
            logging.warning(f"Rule 13 未查到状态变化接口，使用 fallback 查询")
            sql_fallback = f"""
                SELECT 
                    ports.ifName,
                    ports.ifOperStatus,
                    IFNULL(ports.ifOperStatus_prev, 'unknown')
                FROM ports
                WHERE ports.device_id = {device_id}
                  AND ports.ifOperStatus NOT LIKE '%up%'
                LIMIT 20
            """
            result = self.execute_sql(sql_fallback)
            for line in result:
                parts = line.split('\t')
                if len(parts) >= 3:
                    ports_data.append({
                        'ifName': parts[0],
                        'operStatus': parts[1],
                        'prevStatus': parts[2]
                    })
        
        return ports_data

class DingTalkNotifier:
    def __init__(self):
        self.webhooks = Config.DINGTALK_WEBHOOKS
    
    def send(self, content):
        success_count = 0
        for idx, webhook in enumerate(self.webhooks, 1):
            try:
                # 生产环境不添加标记前缀
                data = {
                    "msgtype": "text",
                    "text": {"content": content}
                }
                
                response = requests.post(
                    webhook,
                    json=data,
                    timeout=10
                )
                response.raise_for_status()
                result = response.json()
                
                if result.get('errcode') == 0:
                    logging.info(f"钉钉推送成功 (生产群 {idx})")
                    success_count += 1
                else:
                    logging.warning(f"钉钉推送失败 (生产群 {idx}): {result}")
            
            except Exception as e:
                logging.error(f"钉钉推送异常 (生产群 {idx}): {e}")
        
        return success_count > 0

class AlertFormatter:
    def __init__(self, db_query):
        self.db_query = db_query
    
    def format_duration(self, seconds):
        """格式化持续时间"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        if hours > 0:
            return f"{hours}小时{minutes}分钟"
        else:
            return f"{minutes}分钟"
    
    def format_detail(self, alert, is_repeat=False, duration_seconds=0, previous_ports=None, recovered_ports=None):
        """
        详细告警格式（Rule 13 专用增量检测 + 减量恢复）
        duration_seconds: 告警级别的持续时间（从首次告警触发开始）
        recovered_ports: 已恢复的接口列表（减量）
        """
        device = alert.get('hostname')
        device_id = alert.get('device_id')
        rule = alert.get('name')
        rule_id = alert.get('rule_id')
        severity = alert.get('severity')
        timestamp = alert.get('timestamp')
        
        icon = "🔴"
        
        # 补充设备名称和机房位置（数据库查询，不依赖API是否返回这些字段）
        device_info = self.db_query.get_device_info(device_id)
        sysname = device_info.get('sysname') or '未知'
        location = device_info.get('location') or '未知'
        
        # 重复提醒：显示告警级别的持续时间
        if is_repeat:
            duration_str = self.format_duration(duration_seconds)
            repeat_mark = f"[重复提醒] 告警持续: {duration_str}\n\n"
        else:
            repeat_mark = ""
        
        content = f"""{icon} {repeat_mark}网络告警通知
设备: {device} ({sysname})
位置: {location}
规则: {rule}
严重程度: {severity}
告警确认时间: {timestamp}"""
        
        # Rule 13 专用增量检测逻辑
        if rule_id == 13:
            current_ports_data = self.db_query.get_failed_ports_rule13(device_id)
            
            if not current_ports_data:
                content += "\n\n说明: 未查询到故障接口"
                alert['_rule13_ports'] = {}
                return content
            
            # 当前故障接口集合
            current_ports_dict = {p['ifName']: p for p in current_ports_data}
            
            # 如果没有历史数据，全部视为新增
            if previous_ports is None:
                previous_ports = {}
            
            # 计算增量
            new_ports = []      # 新增故障接口
            existing_ports = [] # 持续故障接口
            current_time = datetime.now()
            
            for ifName, port_data in current_ports_dict.items():
                if ifName not in previous_ports:
                    # 新增故障
                    new_ports.append(port_data)
                    # 记录首次发现时间（脚本检测时间）
                    port_data['first_seen'] = current_time.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    # 持续故障，保留首次发现时间
                    existing_ports.append(port_data)
                    port_data['first_seen'] = previous_ports[ifName].get('first_seen',
                                                                         current_time.strftime('%Y-%m-%d %H:%M:%S'))
            
            # 构建消息内容
            content += "\n\n故障接口:"
            
            # 显示新增故障
            if new_ports:
                content += "\n\n【新增故障】"
                for port in new_ports:
                    first_seen = port['first_seen']
                    content += f"\n- {port['ifName']} (Oper:{port['operStatus']}/Prev:{port['prevStatus']}) 首次检测:{first_seen}"
            
            # 显示持续故障
            if existing_ports:
                content += "\n\n【持续故障】"
                for port in existing_ports:
                    first_seen = port['first_seen']
                    content += f"\n- {port['ifName']} (Oper:{port['operStatus']}) 首次检测:{first_seen}"
            
            # 统计信息
            total_count = len(current_ports_dict)
            new_count = len(new_ports)
            content += f"\n\n总故障接口数: {total_count}"
            if new_count > 0:
                content += f" (新增 {new_count} 个)"
            
            # 显示减量恢复信息
            if recovered_ports:
                content += "\n\n【已恢复接口】"
                for port_info in recovered_ports:
                    ifName = port_info['ifName']
                    first_seen = port_info['first_seen']
                    recovered_at = port_info['recovered_at']
                    
                    # 计算故障持续时长
                    try:
                        first_time = datetime.strptime(first_seen, '%Y-%m-%d %H:%M:%S')
                        recover_time = datetime.strptime(recovered_at, '%Y-%m-%d %H:%M:%S')
                        duration = (recover_time - first_time).total_seconds()
                        duration_str = self.format_duration(duration)
                        content += f"\n- {ifName} (故障时长: {duration_str})"
                    except:
                        content += f"\n- {ifName}"
            
            # 保存当前端口状态（供下次增量检测）
            alert['_rule13_ports'] = current_ports_dict
        
        else:
            # 其他规则保持基础逻辑
            logging.warning(f"Rule {rule_id} 没有定义详细查询函数")
            content += "\n\n说明: 此规则未配置详细查询"
        
        return content
    
    def format_basic(self, alert, is_repeat=False, duration_seconds=0):
        """基础告警格式"""
        device = alert.get('hostname')
        device_id = alert.get('device_id')
        rule = alert.get('name')
        severity = alert.get('severity')
        timestamp = alert.get('timestamp')
        
        icon = "🔴"
        
        # 补充设备名称和机房位置（数据库查询，不依赖API是否返回这些字段）
        device_info = self.db_query.get_device_info(device_id)
        sysname = device_info.get('sysname') or '未知'
        location = device_info.get('location') or '未知'
        
        if is_repeat:
            duration_str = self.format_duration(duration_seconds)
            repeat_mark = f"[重复提醒] 告警持续: {duration_str}\n\n"
        else:
            repeat_mark = ""
        
        return f"""{icon} {repeat_mark}网络告警通知
设备: {device} ({sysname})
位置: {location}
规则: {rule}
严重程度: {severity}
告警确认时间: {timestamp}"""
    
    def format_recovery(self, device, device_id, rule, alert_id, duration_seconds=0):
        """恢复通知格式"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        icon = "🟢"
        duration_str = ""
        if duration_seconds > 0:
            duration_str = f"\n告警持续: {self.format_duration(duration_seconds)}"
        
        # 补充设备名称和机房位置
        device_info = self.db_query.get_device_info(device_id)
        sysname = device_info.get('sysname') or '未知'
        location = device_info.get('location') or '未知'
        
        return f"""{icon} 网络告警恢复
设备: {device} ({sysname})
位置: {location}
规则: {rule}
恢复时间: {now}{duration_str}"""

class StateManager:
    def __init__(self):
        self.state_file = Config.STATE_FILE
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
    
    def load(self):
        if not self.state_file.exists():
            return {}
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"读取状态文件失败: {e}")
            return {}
    
    def save(self, state):
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"保存状态文件失败: {e}")

class AlertProcessor:
    def __init__(self):
        self.api = LibreNMSAPI()
        self.db_query = DatabaseQuery()
        self.notifier = DingTalkNotifier()
        self.formatter = AlertFormatter(self.db_query)
        self.state_manager = StateManager()
    
    def is_detail_rule(self, rule_id):
        return rule_id in Config.DETAIL_RULE_IDS
    
    def should_repeat_notify(self, alert_data, severity):
        """
        判断是否需要重复提醒（仅 critical）
        返回: (是否提醒, 告警持续秒数)
        """
        if severity.lower() != 'critical':
            return False, 0
        
        first_time_str = alert_data.get('first_notify_time')
        if not first_time_str:
            return True, 0
        
        # 计算从首次告警触发到现在的持续时间
        first_time = datetime.strptime(first_time_str, '%Y-%m-%d %H:%M:%S')
        now = datetime.now()
        elapsed = (now - first_time).total_seconds()
        
        # 检查距离上次提醒的时间
        last_notify_str = alert_data.get('last_notify_time')
        if last_notify_str:
            last_notify = datetime.strptime(last_notify_str, '%Y-%m-%d %H:%M:%S')
            since_last = (now - last_notify).total_seconds()
            if since_last >= Config.REPEAT_INTERVAL:
                return True, elapsed
        
        return False, elapsed
    
    def has_port_increment(self, alert_data, rule_id):
        """
        检测 Rule 13 是否有接口增量
        返回: (是否有增量, 新增接口数)
        """
        if rule_id != 13:
            return False, 0
        
        previous_ports = alert_data.get('_rule13_ports', {})
        
        # 查询当前故障接口
        device_id = alert_data.get('device_id')
        current_ports_data = self.db_query.get_failed_ports_rule13(device_id)
        current_ports = {p['ifName'] for p in current_ports_data}
        
        previous_ports_set = set(previous_ports.keys())
        
        # 计算新增
        new_ports = current_ports - previous_ports_set
        if new_ports:
            logging.info(f"Rule 13 检测到增量: 新增 {len(new_ports)} 个接口 {list(new_ports)}")
            return True, len(new_ports)
        
        return False, 0
    
    def check_port_recovery_rule13(self, alert_data, current_ports_data):
        """
        Rule 13 专用：检测接口恢复（减量）
        返回: (是否有恢复, 恢复的接口列表)
        """
        previous_ports = alert_data.get('_rule13_ports', {})
        
        # 当前故障接口集合
        current_ports_set = {p['ifName'] for p in current_ports_data}
        
        # 历史故障接口集合
        previous_ports_set = set(previous_ports.keys())
        
        # 计算减量（已恢复的接口）
        recovered_ports_names = previous_ports_set - current_ports_set
        
        if recovered_ports_names:
            recovered_list = []
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            for ifName in recovered_ports_names:
                port_data = previous_ports[ifName]
                first_seen = port_data.get('first_seen', '未知')
                recovered_list.append({
                    'ifName': ifName,
                    'first_seen': first_seen,
                    'recovered_at': current_time
                })
            
            logging.info(f"Rule 13 检测到减量恢复: {len(recovered_list)} 个接口 {list(recovered_ports_names)}")
            return True, recovered_list
        
        return False, []
    
    def process_recovery(self, previous_state, current_ids):
        """处理告警恢复（整个告警消失）"""
        for alert_id, alert_data in previous_state.items():
            if alert_id not in current_ids:
                device = alert_data.get('hostname')
                device_id = alert_data.get('device_id')
                rule = alert_data.get('name')
                
                # 计算告警持续时间（从首次触发到恢复）
                duration_seconds = 0
                first_time_str = alert_data.get('first_notify_time')
                if first_time_str:
                    first_time = datetime.strptime(first_time_str, '%Y-%m-%d %H:%M:%S')
                    now = datetime.now()
                    duration_seconds = (now - first_time).total_seconds()
                
                logging.info(f"告警恢复: ID={alert_id} 设备={device} 持续={duration_seconds}秒")
                content = self.formatter.format_recovery(device, device_id, rule, alert_id, duration_seconds)
                self.notifier.send(content)
    
    def process_new_alerts(self, alerts, previous_state):
        """处理新告警"""
        new_state = {}
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        for alert in alerts:
            alert_id = str(alert.get('id'))
            rule_id = alert.get('rule_id')
            severity = alert.get('severity', 'warning')
            
            is_new = alert_id not in previous_state
            is_repeat = False
            is_increment = False
            has_recovery = False
            duration_seconds = 0
            recovered_ports = None
            
            if is_new:
                # 全新告警
                logging.info(f"新告警: ID={alert_id} Rule={rule_id} Severity={severity}")
                should_notify = True
                alert['first_notify_time'] = current_time
                previous_ports = None
            
            else:
                # 已存在的告警
                previous_ports = previous_state[alert_id].get('_rule13_ports')
                
                # Rule 13: 检测减量恢复
                if rule_id == 13:
                    device_id = alert.get('device_id')
                    current_ports_data = self.db_query.get_failed_ports_rule13(device_id)
                    has_recovery, recovered_ports = self.check_port_recovery_rule13(
                        previous_state[alert_id],
                        current_ports_data
                    )
                
                # 检查接口增量（Rule 13 专用）
                has_increment, increment_count = self.has_port_increment(previous_state[alert_id], rule_id)
                
                if has_increment or has_recovery:
                    # 有增量或减量，立即推送
                    if has_increment:
                        logging.info(f"增量告警: ID={alert_id} 新增 {increment_count} 个接口")
                    if has_recovery:
                        logging.info(f"减量恢复: ID={alert_id} 恢复 {len(recovered_ports)} 个接口")
                    
                    should_notify = True
                    is_increment = True
                    # 保留首次告警时间
                    alert['first_notify_time'] = previous_state[alert_id].get('first_notify_time', current_time)
                else:
                    # 无增量无减量，检查是否需要重复提醒
                    should_notify, duration_seconds = self.should_repeat_notify(previous_state[alert_id], severity)
                    if should_notify:
                        logging.info(f"重复提醒: ID={alert_id} 告警持续={duration_seconds}秒")
                        is_repeat = True
                        # 保留首次告警时间
                        alert['first_notify_time'] = previous_state[alert_id].get('first_notify_time', current_time)
                    else:
                        # 不需要通知，保留旧数据
                        alert = previous_state[alert_id].copy()
                        new_state[alert_id] = alert
                        continue
            
            if should_notify:
                # 格式化并推送
                if self.is_detail_rule(rule_id):
                    logging.info(f"详细处理: Rule {rule_id} (新={is_new}, 增量={is_increment}, 减量={has_recovery}, 重复={is_repeat})")
                    content = self.formatter.format_detail(
                        alert, 
                        is_repeat, 
                        duration_seconds, 
                        previous_ports,
                        recovered_ports
                    )
                else:
                    logging.info(f"基础处理: Rule {rule_id}")
                    content = self.formatter.format_basic(alert, is_repeat, duration_seconds)
                
                self.notifier.send(content)
                logging.info(f"✅ 已推送: ID={alert_id}")
                
                # 更新最后提醒时间
                alert['last_notify_time'] = current_time
            
            new_state[alert_id] = alert
        
        return new_state
    
    def run(self):
        logging.info("=== 生产环境 === 开始查询告警")
        
        response = self.api.get_alerts()
        if not response or response.get('status') != 'ok':
            logging.error("API 响应异常")
            return
        
        alerts = response.get('alerts', [])
        alert_count = len(alerts)
        logging.info(f"活动告警数: {alert_count}")
        
        previous_state = self.state_manager.load()
        current_ids = {str(alert['id']) for alert in alerts}
        
        self.process_recovery(previous_state, current_ids)
        
        if alerts:
            new_state = self.process_new_alerts(alerts, previous_state)
            self.state_manager.save(new_state)
        else:
            self.state_manager.save({})
            logging.info("状态已清空")
        
        logging.info("=== 生产环境 === 处理完成")

def main():
    setup_logging()
    check_required_config()
    print("=" * 50)
    print("生产环境告警脚本 v11.0")
    print("Rule 13: 增量检测 + 减量恢复 + 告警级别持续时间")
    print("=" * 50)
    
    try:
        processor = AlertProcessor()
        processor.run()
    except Exception as e:
        logging.exception(f"脚本执行异常: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
