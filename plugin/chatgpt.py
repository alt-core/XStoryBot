# coding: utf-8

import commands


CHATGPT_CMDS = (u'@chatgpt', u'@ChatGPT')

#import requests
import json
import logging

from google.appengine.api import urlfetch

class ChatGPTPlugin_Runtime(object):
    def __init__(self, params):
        self.params = params
        self.api_key = params['api_key']
        self.model = params.get('model', 'gpt-3.5-turbo')
        self.base_url = params.get('base_url', 'https://api.openai.com/v1/chat/')
        self.max_response_length = params.get('max_response_length', 300) + 0
        self.max_history = params.get('max_history', 6) + 0
        self.headers = {
            'Content-Type': 'application/json; charset=UTF-8',
            'Authorization': 'Bearer {}'.format(self.api_key),
        }


    def post_chatgpt(self, endpoint, data):
        url = self.base_url + endpoint
        #print(url)
        #print(json.dumps(data, ensure_ascii=False).encode('utf8'))
        #return requests.post(url, headers=self.headers, data=json.dumps(data, ensure_ascii=False).encode('utf8'), timeout=25)
        return urlfetch.fetch(
            url,
            method=urlfetch.POST,
            payload=json.dumps(data, ensure_ascii=False).encode('utf-8'),
            headers=self.headers,
            deadline=120,
        )
    
    def call_chatgpt_chat(self, system_message, user_message, history=[]):
        messages = [{"role": "system", "content": system_message}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})
        data = {
            "model": self.model,
            "messages": messages
        }
        response = self.post_chatgpt('completions', data)
        #response.raise_for_status() # TODO: 丁寧なエラー処理
        #response_json = response.json()
        if response.status_code != 200:
            logging.error(u'Failed to request ChatGPT API: {0}'.format(response.status_code))
            return None
        response_json = json.loads(response.content)
        try:
            response_message = response_json['choices'][0]['message']['content'].strip()
        except Exception:
            logging.error(u'Failed to parse response of ChatGPT API: {0}'.format(response_json))
            return None
        return response_message

    def run_command(self, context, sender, msg, options):
        if msg in CHATGPT_CMDS:
            system_message = options[0]
            user_message = options[1]
            variable = options[2] if len(options) > 2 else None
            history = []
            if variable:
                history = json.loads(context.status.get(variable, u'{"h": []}'))['h']
            agent_message = self.call_chatgpt_chat(system_message, user_message, history)
            if agent_message:
                while agent_message.startswith(u'@'):
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
            return True

        return False


def load_plugin(params):
    builder = commands.Default_Builder()
    runtime = ChatGPTPlugin_Runtime(params)
    commands.register_command(commands.CommandEntry(
        names=CHATGPT_CMDS,
        options='raw raw [variable]',
        builder=builder,
        runtime=runtime,
        service='*'))
