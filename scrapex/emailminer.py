
import re
from scrapex import common, Doc

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

import logging

logger = logging.getLogger(__name__)


def mine_emails(url, br, deep_level=2):
	if not url: return []
	if not common.subreg(url, '^(http)'):
		url = 'http://'+url
	if '@' in url:
		return common.get_emails(url)

	domain = common.get_domain(url).lower()

	history = {}

	def _load_page(page_url, current_level):
		"""
		Please make sure this _url is not loaded yet, to avoid loaded twice

		"""
		logger.debug('mine_emails page %s, level %s', page_url, current_level)

		try:

			br.get(page_url)

		except:
			pass

		html = br.page_source
		doc = Doc(url=page_url, html=html)
		#update loaded links
		links = doc.q("//a")
		
		sub_urls = []

		for link in links:
			_url = link.href()

			if domain not in _url.lower():
				continue

			if _url in history:
				continue
			if _url not in sub_urls:	
				sub_urls.append(_url)	

		history[page_url] = (current_level+1, sub_urls)


		return doc		
				


	def _parse_emails(doc):
		emails = []
		#firstly try to get emails from the links only because it's more reliable
		link_texts = doc.q("//a").join(' | ')
		
		for email in common.get_emails(link_texts):
		
			if '@' in email and email not in emails:
				emails.append(email)

		if not emails:
			#try with text only, not links
			html = doc.remove("//script").html()
			for email in common.get_emails(html):
		
				if '@' in email and email not in emails:
					emails.append(email)	
		return emails			

	def _load_subpages(level):
		#firstly, compile all the urls of this level in the history
		urls = []
		for url in history:
			_level, suburls = history[url]
			if _level != level:
				continue

			for suburl in suburls:
				if suburl in history:
					continue

				if suburl not in urls:
					urls.append(suburl)	

		logger.debug('mine emails in level %s, with %s urls to process', level, len(urls))
		for suburl in urls:			
		
			doc = _load_page(suburl, level)
			emails = _parse_emails(doc)
			if emails:
				#found emails on this page, enough
				return emails

		#not found
		return []		

	
	doc = _load_page(url, current_level = 1)
	emails = _parse_emails(doc)
	
	if emails:
		return emails

	contact_url = doc.x("//a[contains(@href,'contact') or contains(@href,'Contact')]/@href")
	doc = _load_page(contact_url, current_level = 2)
	emails = _parse_emails(doc)
	
	if emails:
		return emails


	#try with level 2

	if deep_level >=2:
		emails = _load_subpages(level=2)
		if emails:
			return emails

	#try with level 3

	if deep_level >=3:
		emails = _load_subpages(level=3)
		if emails:
			return emails

	
	#not found
	return []		
				


def mine_batch(db, cc=3, headless = True, retries = 3, batchsize = 200):
	"""
	mine emails for a db, and update directly

	"""
	
	maxtries = 3
	
	logger.info('items with websites: %s', db._db.items.count({
		
		'$and': [
			{'website': {'$exists':True} },

			{'website': {'$ne':''} },

			{'website': {'$ne': None} },

			]

		}))

	
	logger.info('items minded successfully: %s', db._db.items.count( 
		{'_mined_emails': True },

		))



	def _worker((items, br)):
		for item in items:
			try:
				website = item.get('website') or item.get('Website')
				item['email'] = mine_emails(website, br)
				item['_mined_emails'] = True
				db.update_item(item)
				
			except Exception as e:
				logger.warn('failed to mine_emails for %s', item['_id'])
				item['_mined_emails'] = u'failed: {}'.format(e.message)
				db.update_item(item)				
				# logger.exception(e)	

	def _pending_items():
		"""
		get the pending items to mine emails for

		"""
		items = []
		for item in db._db.items.find():
			if len(items) >= batchsize:
				break
			if not item.get('website'):
				continue

			if item.get('email'):
				#already done
				continue

			if item.get('_mined_emails'):
				#already done, including failed one
				continue
				
			items.append(item)	

		return items	

	def _reset_failed_items():	
		
		filter_failed_items = {'_mined_emails': {'$regex': re.compile( '.*failed.*', re.I) }}

		db._db.items.update_many(filter_failed_items, {'$set': {'_mined_emails': None}})

		logger.info('reset failed items')

	brs = []	

	def _init_brs():
		#create one br instance per thread
		global brs
		brs = []

		for i in range(0,cc):
			chrome_options = Options()
			if headless:
				chrome_options.add_argument("--headless")

			#todo: adding page load timeout to each br instance

			br = webdriver.Chrome(chrome_options=chrome_options)
			brs.append(br)
	def _quit_brs():
		for br in brs:
			try:
				br.quit()
			except:
				pass
				
	num_of_rounds = 1 + retries
		
	for _round in range(1, num_of_rounds+1):	
		
		logger.info('mine_batch, round: %s', _round)

		
		try:
			
			#start mining
			
			pending_items = None

			batch_no = 0
			while True:

				batch_no += 1
				skip_failed_items = True 
				pending_items = _pending_items()
				if not pending_items:
					break

				logger.info('mine_batch, round: %s, batch#: %s | items: %s', _round, batch_no, len(pending_items))

				_init_brs()

				parts = [part for part in chunks(pending_items, cc)]
				parts = zip(parts, brs) # assign one br for each part
				
				common.start_threads(parts, _worker, cc=cc )

				_quit_brs() #restart brs after each batch


		except Exception as e:
			logger.exception(e)		
		finally:
			_quit_brs()			

		if _round < num_of_rounds:
			#not last round, reset the failed items
			_reset_failed_items()



	

def chunks(_list, no_of_parts):
	""" 
	split a list into mulitple equal parts

	"""
	
	_fake_cnt = 0
	if (len(_list) % no_of_parts) > 0:
		_fake_cnt = no_of_parts - (len(_list) % no_of_parts)
	_fakes = []
	if _fake_cnt >0:

		_fakes = ['__fake__' for i in xrange(_fake_cnt)]

	
	_list += _fakes
	assert len(_list) % no_of_parts == 0
	partsize = len(_list) / no_of_parts

	parts = []
	for i in range(0, len(_list), partsize):
		part = _list[i:i + partsize]
		if part[-1] == '__fake__':
			part = part[0: part.index('__fake__')]
		
		yield part


if __name__ == '__main__':
	# for part in chunks([0,1,2,3,4,5,6,7], 3):
	# 	print part

	l1 = [1,2,3]
	l2 = ['a','b','c']
	print zip(l1,l2)