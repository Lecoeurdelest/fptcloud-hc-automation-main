import json, pprint

with open("__schema_out.json", encoding="utf-8-sig") as f:
    data = json.load(f)
s = data["provider_schemas"]["registry.terraform.io/fpt-corp/fptcloud"]

print("=== RESOURCE: fptcloud_tagging ===")
pprint.pprint(s["resource_schemas"]["fptcloud_tagging"]["block"])

print()
print("=== DATASOURCE: fptcloud_tagging ===")
pprint.pprint(s["data_source_schemas"]["fptcloud_tagging"]["block"])

print()
print("=== RESOURCE: fptcloud_instance attrs ===")
attrs = s["resource_schemas"]["fptcloud_instance"]["block"]["attributes"]
for k, v in attrs.items():
    t = v.get("type")
    flags = []
    for f in ("optional", "required", "computed", "sensitive"):
        if v.get(f):
            flags.append(f)
    print(f"  {k}: {t}  [{', '.join(flags)}]")

print()
print("=== RESOURCE: fptcloud_instance block_types ===")
blocks = s["resource_schemas"]["fptcloud_instance"]["block"].get("block_types", {})
for k, v in blocks.items():
    print(f"  {k}: {v}")
