-- ============================================================
-- 完整操作顺序：先禁用 discovery，再插入 processor 记录
-- 顺序不可颠倒 —— 如果先插入记录、后禁用，中间任何一次 discovery
-- (自动调度或人工触发) 都会把刚插入的记录清空。
-- ============================================================

-- 第 1 步：禁用该设备该模块的 discovery（不影响 poll）
-- 依据: LibreNMS/Util/ModuleList.php::moduleStatus() 读取
--       $device->getAttrib("discover_processors")
-- 注意: 这条规则可以被 `lnms device:discover -m processors <id>`
--       这种显式指定模块的命令绕过，日常自动调度和不带 -m 的
--       常规 discovery 不受影响。
INSERT INTO devices_attribs (device_id, attrib_type, attrib_value)
VALUES ({{DEVICE_ID}}, 'discover_processors', '0');

-- 第 2 步：插入 processor 采集记录
-- 占位符说明:
--   {{DEVICE_ID}}      目标设备在 devices 表里的 device_id
--   {{ENT_PHYS_INDEX}} 该 CPU/板卡对应的 entPhysicalIndex（用
--                      ENTITY-MIB::entPhysicalName 反查确认，别猜）
--   {{FULL_OID}}       完整数字 OID（不要用符号名，符号名会被本地
--                      MIB 文件翻译到错误的企业号）
--   {{DESCR}}          描述文字，建议和同型号/同角色设备的现有命名
--                      习惯保持一致（如 "MPU Board 0 Processor"）
--   {{PRECISION}}      精度除数，通常为 1；如果实测数值明显偏大/
--                      偏小，酌情调整为 10 / 100
INSERT INTO processors
    (entPhysicalIndex, hrDeviceIndex, device_id, processor_oid, processor_index,
     processor_type, processor_usage, processor_descr, processor_precision, processor_perc_warn)
VALUES
    ({{ENT_PHYS_INDEX}}, NULL, {{DEVICE_ID}}, '{{FULL_OID}}', '{{ENT_PHYS_INDEX}}',
     'manual', 0, '{{DESCR}}', {{PRECISION}}, 75);

-- 第 3 步：验证（在 LibreNMS 容器内，用运行 LibreNMS 的系统用户执行）
--   1) 确认discovery被挡住:
--      lnms device:discover -vv {{DEVICE_ID}}
--      日志中应【不再出现】"#### Load discovery module processors ####"
--   2) 确认poll正常刷新:
--      lnms device:poll -m processors -vv {{DEVICE_ID}}
--      确认输出中出现 SNMP 请求日志、{{DESCR}} 对应的数值、以及对应
--      RRD 文件的 create/update 记录。
