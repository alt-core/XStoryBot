# coding: utf-8
import yaml
import sys
import re
import json

regex = re.compile(sys.argv[1])

logs = yaml.load_all(sys.stdin)
print('"date","user","category","log","scene","action"'.encode('utf-8-sig'))
for log in logs:
  lines = log.get('protoPayload', {}).get('line', [])
  for line in lines:
    try:
      log_dict = json.loads(line['logMessage'])
    except ValueError as e:
      continue
    if log_dict.get("type", None) != "XSBLog":
      continue
    if regex.search(log_dict.get("cat", "")):
      if isinstance(log_dict["log"], list):
        log_dict["log"] = u",".join(log_dict["log"])
      columns = [log_dict[k].replace('"', '""').replace("\n", "\\n") for k in ("date", "uid", "cat", "log", "scene", "action")]
      row = '"' + '","'.join(columns) + '"'
      print(row.encode('utf-8'))

  #print(yaml.dump(log, allow_unicode=True))
