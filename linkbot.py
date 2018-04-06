#!/usr/bin/env python

"""Slackbot to sniff for message snippets that map to resource links.

Configuration:
    Configuration relies on a module named "linkconfig.py"
    that contains:

        1) a module variable API_TOKEN that holds the value for the
           Slack instance acces token
        2) a variable LINKBOTS that is a list of one or more dictionaries
           defining:
            a) MATCH key that is a string within messages to match
            b) LINK that is a slack format link definition

Run linkbot

        $ python linkbot.py
"""
from slacker import Slacker
from websocket import create_connection
from random import choice
import simplejson as json
import re
import linkconfig
from linkbot import clients
from flask import Flask
import threading
from functools import partial
app = Flask(__name__)


class LinkBotSeenException(Exception): pass


class LinkBot(object):
    """Implements Slack message matching and link response

    """
    QUIPS = [
        '%s',
        'linkbot noticed a link!  %s',
        'Oh, here it is... %s',
        'Maybe this, %s, will help?',
        'Click me!  %s',
        'Click my shiny metal link!  %s',
        'Here, let me link that for you... %s',
        'Couldn\'t help but notice %s was mentioned...',
        'Not that I was eavesdropping, but did you mention %s?',
        'hmmmm, did you mean %s?',
        '%s...  Mama said there\'d be days like this...',
        '%s?  An epic, yet approachable tale...',
        '%s?  Reminds me of a story...',
    ]

    def __init__(self, conf):
        self._conf = conf
        self._match = conf.get('MATCH')
        self._quips = conf.get('QUIPS', self.QUIPS)
        self._link = conf.get('LINK', '%s|%s')
        self._quiplist = []
        self._seen = []

    def match(self, text):
        return re.findall(r'(\A|\W)(%s)(\W|\Z)' % self._match, text, flags=re.I)

    def message(self, link_label):
        if link_label in self._seen:
            raise LinkBotSeenException(link_label)

        self._seen.append(link_label)
        return self._message_text(self._link % (link_label, link_label))

    def reset(self):
        self._seen = []

    def _quip(self, link):
        try:
            if not len(self._quiplist):
                self._quiplist = self._quips

            quip = choice(self._quiplist)
            self._quiplist.remove(quip)
            return quip % link
        except IndexError:
            pass

        return link

    def _message_text(self, link):
        return self._quip(link)

    def _escape_html(self, text):
        escaped = {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
        }

        return "".join(escaped.get(c,c) for c in text)


class JiraLinkBot(LinkBot):
    """Subclass LinkBot to customize response for JIRA links

    """
    def __init__(self, conf):
        super(JiraLinkBot, self).__init__(conf)
        self.jira = clients.UwSamlJira(host=conf.get('HOST'),
                                       auth=conf.get('AUTH'))

    def message(self, link_label):
        msg = super(JiraLinkBot, self).message(link_label)
        try:
            issue = self.jira.issue(link_label)
            summary = issue.fields.summary
            get_name = lambda person: person and person.displayName or 'None'
            reporter = '*Reporter* ' + get_name(issue.fields.reporter)
            assignee = '*Assignee* ' + get_name(issue.fields.assignee)
            status = '*Status* ' + issue.fields.status.name
            lines = list(map(self._escape_html,
                             [summary, reporter, assignee, status]))
            msg = '\n> '.join([msg] + lines)
        except Exception as e:
            print(e)
        return msg


class ServiceNowBot(LinkBot):
    def __init__(self, conf):
        super(ServiceNowBot, self).__init__(conf)
        self.client = clients.ServiceNowClient(
            host=conf.get('HOST'), auth=conf.get('AUTH'))

    def message(self, link_label):
        record = self.client.get(link_label)
        link = self._strlink(link_label)
        lines = [self._quip(link)]
        for key, value in record.items(pretty_names=True):
            if key == 'Subject':
                lines.append(value or 'No subject')
            elif key == 'Parent' and value:
                link = self._strlink(value)
                lines.append('*{key}* {link}'.format(key=key, link=link))
            elif value and key != 'Number':
                lines.append('*{key}* {value}'.format(key=key, value=value))
        return '\n> '.join(lines)

    def _strlink(self, link_label):
        link = self.client.link(link_label)
        return '<{link}|{label}>'.format(link=link, label=link_label)


def get_message_processor(slack):
    robo_id = slack.auth.test().body.get('user_id')

    link_bots = []
    for bot_conf in getattr(linkconfig, 'LINKBOTS', []):
        bot_class = globals()[bot_conf.get('LINK_CLASS', 'LinkBot')]
        link_bots.append(bot_class(bot_conf))

    if not len(link_bots):
        raise Exception('No linkbots defined')

    def process_slack_message(json_string):
        j = json.loads(json_string)

        if j['type'] == 'message':
            if j.get('bot_id'):  # ignore all bots
                return
        
            for bot in link_bots:
                for match in bot.match(j['text']):
                    print(j['text']+ " match!")
                    try:
                        slack.chat.post_message(
                            j['channel'],
                            bot.message(match[1]),
                            as_user=robo_id,
                            parse='none')
                    except LinkBotSeenException:
                        pass
                bot.reset()
    return process_slack_message


def linkbot():
    """Establish Slack connection and filter messages
    
    """
    slack = Slacker(getattr(linkconfig, 'API_TOKEN'))
    process_slack_message = get_message_processor(slack)
    response = slack.rtm.start()
    websocket = create_connection(response.body['url'])
    try:
        while True:
            try:
                rcv = websocket.recv()
                process_slack_message(rcv)
            except KeyError:
                pass
    finally:
        websocket.close()

from queue import Queue

q = Queue()
NETID_GEN = (f'NETID-{i}' for i in range(900, 1100))

@app.route('/', methods=['POST'])
def index():
    message = request.get_json()
    message = json.dumps({'type': 'message', 'text': next(NETID_GEN), 'channel': '#jpf-throwaway'})
    q.put(message)
    return message


def worker(process_message):
    while True:
        message = q.get()
        app.logger.error(f'we got {message}')
        if not message:
            break
        process_message(message)
        q.task_done()


threads = []
for _ in range(2):
    t = threading.Thread(target=partial(worker, get_message_processor(Slacker(linkconfig.API_TOKEN))))
    t.start()
    threads.append(t)


# this allows us to load this script through mod_wsgi
application = app


if __name__ == '__main__':
    linkbot()
