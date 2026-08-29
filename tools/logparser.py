# coding: utf-8
import yaml
import sys
import re
import json
import codecs

regex = re.compile(sys.argv[1])

logs = yaml.safe_load_all(sys.stdin)
sys.stdout = codecs.getwriter('utf-8-sig')(sys.stdout.buffer)
print('"date","user","category","log","scene","action"')
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
                log_dict["log"] = ",".join(log_dict["log"])
            columns = [log_dict[k].replace('"', '""').replace("\n", "\\n") for k in ("date", "uid", "cat", "log", "scene", "action")]
            row = '"' + '","'.join(columns) + '"'
            print(row)

    #print(yaml.dump(log, allow_unicode=True))
