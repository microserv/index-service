# -*- coding: utf-8 -*-
from __future__ import print_function
from HTMLParser import HTMLParser
from twisted.web import server
from twisted.web.resource import Resource
from twisted.web.client import Agent
from twisted.internet.defer import Deferred
from twisted.internet import reactor
from twisted.internet.protocol import Protocol
from database_api import DatabaseAPI
import urllib
import json
import codecs
import re


""" Index microservice class """
class IndexService(Resource):
    isLeaf = True

    def __init__(self, host, port, dbname, user, password, stopword_file_path):
        Resource.__init__(self)
        self.index = DatabaseAPI(host, port, dbname, user, password)
        self.indexer = Indexer(stopword_file_path)
        self.startup_routine()


    # Asks the user for some questions at startup.
    def startup_routine(self):
        indexContent = False
        while True:
            print()
            print("************ Index microservice options: ************")
            print("1. Reset and initialize index database")
            print("2. Index all articles from content service on startup")
            print("3. Start service")
            print("4. Quit")
            print(">> ", end="")
            user_input = str(raw_input())
            print()
            if user_input == '1':
                self.initialize_database()   
            elif user_input == '2':
                self.index_all_articles()
            elif user_input == '3':
                print("Starting index service. Use Ctrl + c to quit.")
                reactor.listenTCP(8001, server.Site(self))
                reactor.run()
                break
            elif user_input == '4':
                break
            else:
                print("Option has to be a number between 1 and 3")
                continue

    # Creates new clean tables in the database.
    def initialize_database(self):
        yes = set(['Y', 'y', 'Yes', 'yes', 'YES'])
        no = set(['N', 'n', 'No', 'no', 'NO'])
        while True:
            print("This will delete any existing data and reset the database.")
            print("Are you sure you want to continue? [y]es/[n]o")
            print(">> ", end="")
            user_input = str(raw_input())
            if user_input in yes:
                self.index.make_tables()
                break
            elif user_input in no:
                break
            else:
                print()
                continue

    # Indexes all articles from the content microservice.
    def index_all_articles(self):
        # **** TODO: dont index already indexed articles ****
        #host = 'http://127.0.0.1:8002'  # content host - **** TODO: fetched from dht node network ****
        host = self.get_service_ip('publish') + "/list"

        yes = set(['Y', 'y', 'Yes', 'yes', 'YES'])
        no = set(['N', 'n', 'No', 'no', 'NO'])
        while True:
            print("Do you want to index all articles at: '" + host + "'?") 
            print("y]es/[n]o: ", end="")
            #print(">> ", end="")
            user_input = str(raw_input())
            if user_input in yes:
                agent = Agent(reactor) 
                d = agent.request("GET", host)
                d.addCallback(self._cbRequest)
                break
            elif user_input in no:
                break
            else:
                print()
                continue

    # Callback request
    def _cbRequest(self, response):
        finished = Deferred()
        finished.addCallback(self._index_content)
        response.deliverBody(RequestClient(finished))

    # Callback for _cbRequest. Indexes the articles in the GET response.
    def _index_content(self, response):
        article_id_list = json.loads(response)['list']
        for i in range(len(article_id_list)):
            print("Indexing article ", i+1, " of ", len(article_id_list))
            article_id = article_id_list[i]['id']
            self.index_article(article_id)
        print("Indexing completed")

    # Indexes page.
    def index_article(self, article_id):
        host = self.get_service_ip('publish')
        url = host+'/article/'+article_id # Articles are found at: http://<publish_module_ip>:<port_num>/article/<article_id> 
        values = self.indexer.make_index(url)
        self.index.upsert(article_id, values)

    # Handles POST requests from the other microservices.
    def render_POST(self, request):
        d = json.load(request.content)
        # When suggestions for words are needed:
        if d['task'] == 'getSuggestions': # JSON format: {'task' : 'getSuggestions', 'word' : str}
            word_root = d['word']
            data = self.index.query("SELECT DISTINCT word FROM wordfreq WHERE word LIKE %s", (word_root+'%',))
            response = {"suggestions" : [t[0] for t in data]}
            return json.dumps(response)
        # When articles with a given word is needed:
        elif d['task'] == 'getArticles': # JSON format: {'task' : 'getArticles', 'word' : str}
            word = d['word']
            data = self.index.query("SELECT articleid FROM wordfreq WHERE word = %s", (word,))
            response = {"articleID" : [t[0] for t in data]}
            return json.dumps(response)
        # When the word frequency list is needed:
        elif d['task'] == 'getFrequencyList': # JSON format: {'task' : 'getFrequencyList'}
            data = self.index.query("SELECT word, sum(frequency) FROM wordfreq GROUP BY word")
            response = {}
            for value in data:
                response[value[0]] = value[1]
            return json.dumps(response)
        # When an article is updated:
        elif d['task'] == 'updatedArticle':
            article_id = d['articleID']
            self.index.remove(article_id)
            self.index.upsert(article_id)
            return 'thanks!'
        # When an article is published:
        elif d['task'] == 'publishedArticle':
            article_id = d['articleID']
            self.index.upsert(article_id)
            return 'thanks!'
        # When an article is removed:
        elif d['task'] == 'removedArticle':
            article_id = d['articleID']
            self.index.remove(article_id)
            return('ok!')
        else:
            return('404')

    # Temporary fuction to fetch a services address. Should connect with the dht node somehow
    def get_service_ip(self, service_name):
        return "http://despina.128.no/publish"


""" Request Client """
class RequestClient(Protocol):
    def __init__(self, finished):
        self.finished = finished

    def dataReceived(self, data):
        self.data = data

    def connectionLost(self, reason):
        self.finished.callback(self.data)  # Executes all registered callbacks.

   
""" Basic indexer of HTML pages """
class Indexer(object):
    stopwords = None

    def __init__(self, stopword_file_path):
        self.stopwords = set([''])
        # Reading in the stopword file:
        with codecs.open(stopword_file_path, encoding='utf-8') as f:
            for word in f:
                self.stopwords.add(unicode(word.strip()))

    # Takes an url as arguments and indexes the article at that url. Returns a list of tuple values.
    def make_index(self, url):
        # Retriving the HTML source from the url:
        '''
        req = urllib2.Request(url)
        response = urllib2.urlopen(req)
        page = response.read() #.decode('UTF-8')
        response.close()
        '''
        page = urllib.urlopen(url).read().decode('utf-8')

        # Parseing the HTML:
        parser = Parser()
        parser.feed(page)
        content = parser.get_content()
        parser.close()

        # Removing stopwords:
        unique_words = set(content).difference(self.stopwords)

        # Making a list of tuples: (word, wordfreq):
        values = []
        for word in unique_words:
            values.append((word, content.count(word)))
        return values


""" Basic parser for parsing of html data """
class Parser(HTMLParser):	
    tags_to_ignore = set(["script"]) # Add HTML tags to the set to ignore the data from that tag.

    def __init__(self):
        HTMLParser.__init__(self)
        self.content = []
        self.ignore_tag = False  

    # Keeps track of which tags to ignore data from.
    def handle_starttag(self, tag, attrs):
        if tag in self.tags_to_ignore:
            self.ignore_tag = True
        else:
            self.ignore_tag = False
      
    # Handles data from tags.  
    def handle_data(self, data):
        if self.ignore_tag:
            return
        words = filter(None, re.split("[ .,:;()!#¤%&=?+`´*_@£$<>^~/\[\]\{\}\-\"\']+", data))
        for word in words:
            if len(word) > 1:
                self.content.append(word.lower().strip())

    # Get method for content.
    def get_content(self):
        return self.content




