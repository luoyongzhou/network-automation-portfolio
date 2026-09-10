from nornir import InitNornir
from pprint import pprint

nr = InitNornir(config_file="config.yaml")

# 打印所有已加载的 Group 名称
print("所有 Group 名称:", nr.inventory.groups.keys())

# 专门检查 h3c_sr88_comware_v7_r7171
target_group = nr.inventory.groups.get("h3c_sr88_comware_v7_r7171")
if target_group:
    print("\n=== h3c_sr88_comware_v7_r7171 的完整配置 ===")
    pprint(target_group.dict())
else:
    print("❌ 找不到该 Group！")