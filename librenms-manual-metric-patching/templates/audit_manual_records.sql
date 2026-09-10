-- ============================================================
-- 查询：找出所有靠"手工插库方法"补齐的记录（processors + mempools）
-- 用于日常审计/清理，避免和 discovery 自动生成的记录混淆
-- ============================================================
SELECT 'processors' AS tbl, p.processor_id AS record_id, p.device_id, d.hostname,
       p.processor_descr AS descr, p.processor_usage AS current_value
FROM processors p
JOIN devices d ON p.device_id = d.device_id
WHERE p.processor_type = 'manual'
UNION ALL
SELECT 'mempools' AS tbl, m.mempool_id AS record_id, m.device_id, d.hostname,
       m.mempool_descr AS descr, m.mempool_perc AS current_value
FROM mempools m
JOIN devices d ON m.device_id = d.device_id
WHERE m.mempool_type = 'manual'
ORDER BY tbl, device_id;

-- ============================================================
-- 巡检：找出"插了手工记录，但没配套写 devices_attribs 禁用规则"的设备
-- 这类设备存在"记录会在下次discovery时被静默清空"的风险，需要补配置
-- ============================================================
SELECT DISTINCT 'processors' AS tbl, p.device_id, d.hostname
FROM processors p
JOIN devices d ON p.device_id = d.device_id
WHERE p.processor_type = 'manual'
  AND p.device_id NOT IN (
      SELECT device_id FROM devices_attribs WHERE attrib_type = 'discover_processors' AND attrib_value = '0'
  )
UNION ALL
SELECT DISTINCT 'mempools' AS tbl, m.device_id, d.hostname
FROM mempools m
JOIN devices d ON m.device_id = d.device_id
WHERE m.mempool_type = 'manual'
  AND m.device_id NOT IN (
      SELECT device_id FROM devices_attribs WHERE attrib_type = 'discover_mempools' AND attrib_value = '0'
  );

-- ============================================================
-- 删除：设备下线或板卡更换后，清理对应记录（连同禁用规则一起清）
-- ============================================================
-- DELETE FROM processors WHERE processor_id = {{PROCESSOR_ID}};
-- DELETE FROM mempools WHERE mempool_id = {{MEMPOOL_ID}};
-- DELETE FROM devices_attribs WHERE device_id = {{DEVICE_ID}} AND attrib_type = 'discover_processors';
-- DELETE FROM devices_attribs WHERE device_id = {{DEVICE_ID}} AND attrib_type = 'discover_mempools';

-- 对应需要手动清理的 RRD 文件路径（不清也不影响功能，只是残留文件）：
--   <rrd_root>/<device_hostname>/processor-manual-<entPhysicalIndex>.rrd
--   <rrd_root>/<device_hostname>/mempool-manual-system-<entPhysicalIndex>.rrd
