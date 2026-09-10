-- ============================================================
-- 完整操作顺序：先禁用 discovery，再插入 mempool 记录
-- 顺序不可颠倒 —— 原理和坑点同 insert_processor.sql，此处不再重复，
-- 完整说明见仓库根目录 README.md 的"内存（mempools 表）同样适用"小节。
-- ============================================================

-- 第 1 步：禁用该设备该模块的 discovery（不影响 poll）
INSERT INTO devices_attribs (device_id, attrib_type, attrib_value)
VALUES ({{DEVICE_ID}}, 'discover_mempools', '0');

-- 第 2 步：插入 mempool 采集记录
-- 占位符说明:
--   {{DEVICE_ID}}       目标设备在 devices 表里的 device_id
--   {{ENT_PHYS_INDEX}}  该内存/板卡对应的 entPhysicalIndex
--   {{PERC_OID}}        内存使用率百分比的完整数字 OID
--   {{TOTAL_OID}}       内存总容量的完整数字 OID —— 务必填，否则 poll
--                       阶段无法反推 used/free，数值会永远停留在初始值
--   {{DESCR}}           描述文字（如 "MPU Board 0 Memory"）
--   {{USED_BYTES}} / {{FREE_BYTES}} / {{TOTAL_BYTES}}
--                       插入时的初始估算值，单位需要和 {{TOTAL_OID}}
--                       返回值单位一致（通常是 bytes）。插入后立即跑一次
--                       poll，LibreNMS 会用 {{PERC_OID}} + {{TOTAL_OID}}
--                       反推出准确值覆盖这里的估算值。
INSERT INTO mempools
    (mempool_index, entPhysicalIndex, mempool_type, mempool_class, mempool_precision,
     mempool_descr, device_id, mempool_perc, mempool_perc_oid,
     mempool_used, mempool_free, mempool_total, mempool_total_oid, mempool_perc_warn, mempool_deleted)
VALUES
    ('{{ENT_PHYS_INDEX}}', {{ENT_PHYS_INDEX}}, 'manual', 'system', 1,
     '{{DESCR}}', {{DEVICE_ID}}, 0, '{{PERC_OID}}',
     {{USED_BYTES}}, {{FREE_BYTES}}, {{TOTAL_BYTES}}, '{{TOTAL_OID}}', 90, 0);

-- 第 3 步：验证
--   1) 确认discovery被挡住:
--      lnms device:discover -vv {{DEVICE_ID}}
--      日志中应【不再出现】"#### Load discovery module mempools ####"
--   2) 确认poll正常刷新:
--      lnms device:poll -m mempools -vv {{DEVICE_ID}}
--      确认输出中 {{DESCR}} 对应的 used/free 数值与手工snmpget验证值一致，
--      且对应 RRD 文件有 create/update 记录。
