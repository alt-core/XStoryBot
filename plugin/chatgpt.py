# coding: utf-8

import commands
import datetime
import pytz


CHATGPT_CMDS = ('@chatgpt', '@ChatGPT')
CHATGPTJSON_CMDS = ('@chatgptjson', '@ChatGPTJSON')

CHATGPT_RESULT_VARIABLE = '$$result'


import requests
import json
import logging


class ChatGPTPlugin_Runtime(object):
    def __init__(self, params):
        self.params = params
        self.api_key = params['api_key']
        self.model = params.get('model', 'gpt-4o')
        self.base_url = params.get('base_url', 'https://api.openai.com/v1/chat/')
        self.max_response_length = params.get('max_response_length', 300) + 0
        self.max_history = params.get('max_history', 6) + 0
        self.log_conversation = params.get('log_conversation', False)
        self.headers = {
            'Content-Type': 'application/json; charset=UTF-8',
            'Authorization': f'Bearer {self.api_key}',
        }
        self.timezone = pytz.timezone(params.get('timezone', 'utc'))

    def post_chatgpt(self, endpoint, data):
        url = self.base_url + endpoint
        #print(url)
        #print(json.dumps(data, ensure_ascii=False).encode('utf8'))
        return requests.post(
            url,
            headers=self.headers,
            data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
            timeout=120
        )

    def call_chatgpt_chat(self, system_message, user_message, history=[], as_json=False):
        messages = [{"role": "system", "content": system_message}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})
        data = {
            "model": self.model,
            "messages": messages,
        }
        if as_json:
            data["response_format"] = {"type": "json_object"}
        response = self.post_chatgpt('completions', data)
        #response.raise_for_status() # TODO: 丁寧なエラー処理
        #response_json = response.json()
        logging.info(response.status_code)
        logging.info(response.content)
        #print(response.status_code)
        #print(response.content)
        if response.status_code != 200:
            logging.error('Failed to request ChatGPT API: {0}'.format(response.status_code))
            return None
        response_json = response.json()
        try:
            response_message = response_json['choices'][0]['message']['content'].strip()
        except Exception:
            logging.error('Failed to parse response of ChatGPT API: {0}'.format(response_json))
            return None
        return response_message

    def run_command(self, context, sender, msg, options):
        if msg in CHATGPT_CMDS:
            system_message = options[0]
            user_message = options[1]
            variable = options[2] if len(options) > 2 else None
            history = []
            if variable:
                history = json.loads(context.status.get(variable, '{"h": []}'))['h']
            agent_message = self.call_chatgpt_chat(system_message, user_message, history)
            if self.log_conversation:
                timestamp = datetime.datetime.now(tz=self.timezone).strftime('%Y/%m/%d %H:%M:%S')
                uid_str = str(context.user)
                scene_str = context.status.scene
                log = {
                    "type": "XSBLLMLog",
                    "cat": "ChatGPT",
                    "date": timestamp,
                    "uid": uid_str,
                    "user": user_message,
                    "history": history,
                    "out": agent_message,
                    "scene": scene_str,
                }
                logging.info(json.dumps(log))

            if agent_message:
                while agent_message.startswith('@'):
                    agent_message = agent_message[1:]
                agent_message = agent_message[:self.max_response_length]
                context.reactions.append(([sender, agent_message], None))
                if variable:
                    user_message = user_message[:self.max_response_length]
                    history.append({"role": "user", "content": user_message})
                    history.append({"role": "assistant", "content": agent_message})
                    if len(history) > self.max_history:
                        history = history[-self.max_history:]
                    context.status[variable] = json.dumps({'h': history}, ensure_ascii=False)
                context.status[CHATGPT_RESULT_VARIABLE] = True
            else:
                context.status[CHATGPT_RESULT_VARIABLE] = False
            return True
        elif msg in CHATGPTJSON_CMDS:
            system_message = options[0]
            if 'json' not in system_message.lower():
                system_message += '\n# 出力形式\nJSON形式で出力してください。'
            user_message = options[1]
            keys = [key.strip() for key in options[2].split(',')]
            variable = options[3] if len(options) > 3 else None
            history = []
            if variable:
                history = json.loads(context.status.get(variable, '{"h": []}'))['h']
            agent_message = self.call_chatgpt_chat(system_message, user_message, history, True)
            if self.log_conversation:
                timestamp = datetime.datetime.now(tz=self.timezone).strftime('%Y/%m/%d %H:%M:%S')
                uid_str = str(context.user)
                scene_str = context.status.scene
                log = {
                    "type": "XSBLLMLog",
                    "cat": "ChatGPTJSON",
                    "date": timestamp,
                    "uid": uid_str,
                    "user": user_message,
                    "history": history,
                    "out": agent_message,
                    "scene": scene_str,
                }
                logging.info(json.dumps(log))
            try:
                agent_json = json.loads(agent_message)
            except Exception:
                logging.error('Failed to parse response of ChatGPT API: {0}'.format(agent_message))
                context.status[CHATGPT_RESULT_VARIABLE] = False
                return True
            # agent_json から keys を取得
            result = {}
            valid = True
            for key in keys:
                # agent_json[key] が存在したら、数値や str なら str に変換して context.status に保存
                value = agent_json.get(key, None)
                if isinstance(value, str):
                    value = value[:self.max_response_length]
                elif isinstance(value, (int, float)):
                    value = value
                else:
                    logging.warning('invalid chatgpt response: {}: {}'.format(key, value))
                    valid = False
                    break
                result[key] = value
            if valid:
                for key, value in result.items():
                    context.status['$$'+key] = value
                if variable:
                    user_message = user_message[:self.max_response_length]
                    history.append({"role": "user", "content": user_message})
                    history.append({"role": "assistant", "content": agent_message})
                    if len(history) > self.max_history:
                        history = history[-self.max_history:]
                    context.status[variable] = json.dumps({'h': history}, ensure_ascii=False)
                context.status[CHATGPT_RESULT_VARIABLE] = True
            else:
                context.status[CHATGPT_RESULT_VARIABLE] = False
            return True

        return False


def load_plugin(params):
    builder = commands.Default_Builder()
    runtime = ChatGPTPlugin_Runtime(params)
    commands.register_commands([
        commands.CommandEntry(
            names=CHATGPT_CMDS,
            options='raw raw [variable]',
            builder=builder,
            runtime=runtime,
            service='*'),
        commands.CommandEntry(
            names=CHATGPTJSON_CMDS,
            options='raw raw raw [variable]',
            builder=builder,
            runtime=runtime,
            service='*'),
    ])
