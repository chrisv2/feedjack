# -*- coding: utf-8 -*-

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import signals, Avg, Max, Min, Count
from django.db import models, connection
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import smart_unicode

from feedjack import fjcache

import itertools as it, operator as op, functools as ft
from collections import namedtuple, defaultdict, Iterable, Iterator
from datetime import datetime, timedelta
import logging



class Link(models.Model):
	name = models.CharField(_('name'), max_length=100, unique=True)
	link = models.URLField(_('link'), verify_exists=True)

	class Meta:
		verbose_name = _('link')
		verbose_name_plural = _('links')

	class Admin: pass

	def __unicode__(self): return u'%s (%s)' % (self.name, self.link)



SITE_ORDERING = namedtuple( 'SiteOrdering',
	'modified created created_day' )(*xrange(1, 4))

class Site(models.Model):
	name = models.CharField(_('name'), max_length=100)
	url = models.CharField( _('url'),
		max_length=100, unique=True,
		help_text=u'{0}: {1}, {2}'.format(
		smart_unicode(_('Example')),
		u'http://www.planetexample.com',
		u'http://www.planetexample.com:8000/foo' ) )
	title = models.CharField(_('title'), max_length=200)
	description = models.TextField(_('description'))
	welcome = models.TextField(_('welcome'), null=True, blank=True)
	greets = models.TextField(_('greets'), null=True, blank=True)

	default_site = models.BooleanField(_('default site'), default=False)
	posts_per_page = models.PositiveIntegerField(_('posts per page'), default=20)
	order_posts_by = models.PositiveSmallIntegerField(_('order posts by'),
		choices=(
			(SITE_ORDERING.modified, _('Time the post was published.')),
			(SITE_ORDERING.created, _('Time the post was first obtained.')),
			(SITE_ORDERING.created_day,
				_('Day the post was first obtained (for nicer per-feed grouping).')) ),
		default=SITE_ORDERING.modified )
	tagcloud_levels = models.PositiveIntegerField(_('tagcloud level'), default=5)
	show_tagcloud = models.BooleanField(_('show tagcloud'), default=True)

	use_internal_cache = models.BooleanField(_('use internal cache'), default=True)
	cache_duration = models.PositiveIntegerField(_('cache duration'), default=60*60*24,
		help_text=_('Duration in seconds of the cached pages and data.') )

	links = models.ManyToManyField(Link, verbose_name=_('links'),
		null=True, blank=True)
	template = models.CharField(_('template'), max_length=100, null=True,
		blank=True,
		help_text=_('This template must be a directory in your feedjack '
		'templates directory. Leave blank to use the default template.') )

	class Meta:
		verbose_name = _('site')
		verbose_name_plural = _('sites')
		ordering = ('name',)


	@property
	def active_subscribers(self):
		return self.subscriber_set.filter(is_active=True)

	@property
	def active_feeds(self):
		return Feed.objects.filter(subscriber__site=self, subscriber__is_active=True)


	def __unicode__(self): return self.name

	def save(self):
		if not self.template:
			self.template = 'default'
		# there must be only ONE default site
		defs = Site.objects.filter(default_site=True)
		if not defs:
			self.default_site = True
		elif self.default_site:
			for tdef in defs:
				if tdef.id != self.id:
					tdef.default_site = False
					tdef.save()
		self.url = self.url.rstrip('/')
		fjcache.hostcache_set({})
		super(Site, self).save()



FILTER_CR_REBUILD = namedtuple(
	'CrossrefRebuild', 'new all' )(*xrange(2))
FILTER_CR_TIMELINE_MAP = 'created', 'modified' # used to get column name
FILTER_CR_TIMELINE = namedtuple( 'CrossrefTimeline',
	' '.join(FILTER_CR_TIMELINE_MAP) )(*xrange(2))

class FilterBase(models.Model):
	# I had to resist the urge to call it FilterClass or FilterModel

	name = models.CharField(max_length=64, unique=True)
	handler_name = models.CharField( max_length=256, blank=True,
		help_text='Processing function as and import-name, like'
			' "myapp.filters.some_filter" or just a name if its a built-in filter'
			' (contained in feedjack.filters), latter is implied if this field is omitted.<br />'
			' Should accept Post object and optional (or not) parameter (derived from'
			' actual Filter field) and return boolean value, indicating whether post'
			' should be displayed or not.' )
	crossref = models.BooleanField( 'Cross-referencing',
		help_text='Indicates whether filtering results depend on other posts'
			' (and possibly their filtering results) or not.<br />'
			' Note that ordering in which these filters are applied to a posts,'
			' as well as "update condition" should match for any'
			' cross-referenced feeds. This restriction might go away in the future.' )
	crossref_rebuild = models.PositiveSmallIntegerField(
		choices=(
			( FILTER_CR_REBUILD.new,
				'Rebuild newer results, starting from the changed point, but not older than crossref_span' ),
			( FILTER_CR_REBUILD.all,
				'Rebuild last results on any changes to the last posts inside crossref_span' ) ),
		help_text="Neighbor posts' filtering results update condition.",
		default=FILTER_CR_REBUILD.new )
	crossref_timeline = models.PositiveSmallIntegerField(
		choices=(
			(FILTER_CR_TIMELINE.created, 'Time the post was first fetched'),
			( FILTER_CR_TIMELINE.modified,
				'Time of last modification to the post, according to the source' ) ),
		help_text="Which time to use for timespan calculations on rebuild.",
		default=FILTER_CR_TIMELINE.created )
	crossref_span = models.PositiveSmallIntegerField( blank=True, null=True,
		help_text='How many days of history should be re-referenced on post '
			'changes to keep this results conclusive. Performance-quality knob, since'
			' ideally this should be an infinity (indicated by NULL value).' )

	@property
	def handler(self):
		'Handler function'
		from feedjack import filters # shouldn't be imported globally, as they may depend on models
		filter_func = getattr(filters, self.handler_name or self.name, None)
		if filter_func is None:
			if '.' not in self.handler_name:
				raise ImportError('Filter function not found: {0}'.format(self.handler_name))
			filter_module, filter_func = it.imap(str, self.handler_name.rsplit('.', 1))
			filter_func = getattr(__import__(filter_module, fromlist=[filter_func]), filter_func)
		return filter_func

	@property
	def handler_description(self):
		try: doc = self.handler.__doc__
		except ImportError: doc = '<Failed to import handler>'
		return smart_unicode(doc or '')

	def __unicode__(self): return u'{0.name} ({0.handler_name})'.format(self)


class Filter(models.Model):
	base = models.ForeignKey('FilterBase', related_name='filters')
	# feeds (reverse m2m relation from Feed)
	parameter = models.CharField( max_length=512, blank=True, null=True,
		help_text='Parameter keyword to pass to a filter function.<br />Allows to define generic'
			' filtering alghorithms in code (like "regex_filter") and actual filters in db itself'
			' (specifying regex to filter by).<br />Null value would mean that "parameter" keyword'
			' wont be passed to handler at all. See selected filter base for handler description.' )

	@property
	def handler(self):
		'Parametrized handler function'
		return ft.partial(self.base.handler, parameter=self.parameter)\
			if self.parameter else self.base.handler

	@property
	def shortname(self): return self.__unicode__(short=True)
	def __unicode__(self, short=False):
		usage = [self.parameter] if self.parameter else list()
		if not short:
			binding = self.feeds.values_list('shortname', flat=True)
			binding = u', '.join(binding) if len(binding) < 5 else '{0} feeds'.format(len(binding))
			usage.append(u'used on {0}'.format(binding) if binding else 'not used for any feed')
		return u'{0.base.name}{1}'.format(self, u' ({0})'.format(u', '.join(usage)) if usage else '')


class FilterResult(models.Model):
	filter = models.ForeignKey('Filter')
	post = models.ForeignKey('Post', related_name='filtering_results')
	result = models.BooleanField()
	timestamp = models.DateTimeField(auto_now=True)

	def __unicode__(self):
		return u'{0.result} ("{0.post}", {0.filter.shortname} on'\
			u' {0.post.feed.shortname}, {0.timestamp})'.format(self)



FEED_FILTERING_LOGIC = namedtuple('FilterLogic', 'all any')(*xrange(2))


class FeedQuerySet(models.query.QuerySet):
	@property
	def timestamps(self):
		return dict(it.izip( ('modified', 'checked'), self.filter(last_checked__isnull=False)\
			.aggregate(Max('last_modified'), Max('last_checked')).itervalues() ))

class Feeds(models.Manager):
	def get_query_set(self): return FeedQuerySet(self.model)


class Feed(models.Model):
	objects = Feeds()

	feed_url = models.URLField(_('feed url'), unique=True)

	name = models.CharField(_('name'), max_length=100)
	shortname = models.CharField(_('shortname'), max_length=50)

	immutable = models.BooleanField( _('immutable'), default=False,
		help_text=_('Do not update posts that were already fetched.') )
	skip_errors = models.BooleanField( _('skip non-critical errors'),
		default=False, help_text=_('Try to be as tolerant as possible during update.') )
	is_active = models.BooleanField( _('is active'), default=True,
		help_text=_('If disabled, this feed will not be further updated.') )

	title = models.CharField(_('title'), max_length=200, blank=True)
	tagline = models.TextField(_('tagline'), blank=True)
	link = models.URLField(_('link'), blank=True)

	filters = models.ManyToManyField('Filter', blank=True, related_name='feeds')
	filters_logic = models.PositiveSmallIntegerField( 'Composition', choices=(
		(FEED_FILTERING_LOGIC.all, 'Should pass ALL filters (AND logic)'),
		(FEED_FILTERING_LOGIC.any, 'Should pass ANY of the filters (OR logic)') ),
		default=FEED_FILTERING_LOGIC.all )

	# http://feedparser.org/docs/http-etag.html
	etag = models.CharField(_('etag'), max_length=127, blank=True)
	last_modified = models.DateTimeField(_('last modified'), null=True, blank=True)
	last_checked = models.DateTimeField(_('last checked'), null=True, blank=True)

	class Meta:
		verbose_name = _('feed')
		verbose_name_plural = _('feeds')
		ordering = ('name', 'feed_url',)

	def __unicode__(self):
		return u'{0} ({1})'.format( self.name, self.feed_url
			if len(self.feed_url) <= 50 else '{0}...'.format(self.feed_url[:47]) )


	@staticmethod
	def _filters_update_handler_check(sender, instance, **kwz):
		try:
			original = Feed.objects.get(id=instance.id)
			instance._filters_logic_update =\
				instance.filters_logic != original.filters_logic
		except ObjectDoesNotExist: pass # shouldn't really matter
	_filters_logic_update = None

	@staticmethod
	def _filters_update_handler( sender, instance, force=False,
			created=None, # post_save-specific
			model=None, pk_set=list(), reverse=None, action=None, # m2m-specific
			**kwz ):
		m2m_update = reverse is not None
		### Main "crossref-rebuild" function.
		### ALL filter-consistency hooks call it in the end.
		### It MUST make sure that all filtering results are up2date.
		### Logic here is pretty obscure, so I'll try to explain it in comments.
		## Check if this call is a result of actions initiated from
		##  one of the hooks in a higher frame (resulting in recursion).
		## Note that there's a similar check in Feed.update_handler, as a shortcut.
		if Feed._filters_update_handler_lock: return
		## post_save-specific checks (force=False), so it won't be triggered on _every_
		##  Feed save, only those that change "filters_logic" on existing feeds.
		if not force and not m2m_update and (
			created or not instance._filters_logic_update ): return
		## Get set of feeds that are affected by m2m update, note that it's always derived
		##  from instance in case of post_save hook, since it doesn't pass "reverse" keyword.
		related_feeds = set(Feed.objects.filter(id__in=pk_set) if reverse else\
			([instance] if not isinstance(instance, (Iterable, Iterator)) else instance))
		## In case of m2m changes, pre_* hooks (pre_clear, pre_add, pre_delete)
		##  only do crossref ordering consistency check. Updates to results are
		##  delayed to inevitable post_* hooks, when updated feed data will hit db.
		rebuild_spec = 'crossref_timeline', 'crossref_rebuild'
		if m2m_update and action.startswith('pre_'):
			aggregate = dict(it.izip(rebuild_spec, it.repeat(None)))
			for k,v in it.chain.from_iterable(it.imap(
					op.methodcaller('iteritems'), FilterBase.objects.filter( crossref=True,
						filters__feeds__in=related_feeds ).values(*aggregate.iterkeys()) )):
				if aggregate[k] is not None and aggregate[k] != v:
					raise ValidationError( 'Crossref filters ordering and update condition'
						' should match for all cross-referenced feeds. Not matching: {0}'.format(k) )
				else: aggregate[k] = v
			return # validaton success
		## Since these are forced to be the same for all feeds...
		## Note, that they are same just because it's convenient. Otherwise, filtering
		##  results should be rebuild on per-FilterBase basis, not per-Post, which would
		##  certainly eat a lot more resources, at least without any optimizations.
		try:
			rebuild_order, rebuild_spec = FilterBase.objects.filter( crossref=True,
				filters__feeds__in=related_feeds ).values_list(*rebuild_spec)[0]
		except IndexError: # indicates that there are no crossref filters
			rebuild_order = rebuild_spec = None
		else: rebuild_order = 'date_{0}'.format(FILTER_CR_TIMELINE_MAP[rebuild_order])
		## Then there's a matter of directly-affected posts (if any) - their results must be updated
		affected_posts = list(it.chain.from_iterable(
			instance.itervalues() )) if isinstance(instance, dict) else list()
		## Special "preparation" of instance if filtering logic of Feed is updated,
		##  like AND/OR flip for crossref or filters' m2m change.
		## Only valid for m2m_update and post_save hook (when "created is False").
		## That certainly affects every Post of "instance", so they all should be updated.
		## Shouldn't happen too often, hopefully.
		if m2m_update or (created is False and instance._filters_logic_update):
			Feed._filters_update_handler_lock = True # this _is_ recursive!
			tainted = Post.objects.filter(feed__in=related_feeds)
			if rebuild_spec:
				FilterResult.objects.filter(
					post__feed__in=related_feeds, filter__base__crossref=True ).delete()
				tainted = tainted.order_by(rebuild_order) # doesn't matter otherwise
			for post in tainted: post.filtering_result_update()
			Feed._filters_update_handler_lock = False
		else: # build/update results for directly-affected posts, won't rebuild crossref results
			Feed._filters_update_handler_lock = True
			for post in affected_posts: post.filtering_result_update()
			Feed._filters_update_handler_lock = False
		# Shortcut in case there are no affected feeds with crossref filters
		if not rebuild_spec: return
		## Get all Sites, incorporating the feed (all their feeds are affected), then
		##  drop cross-referencing filters' results, as they'd be totally screwed.
		## That means dropping all such results for every feed that shares a Site with "instance".
		# This is a set of feeds that share the Site(s) with "instance" _and_ have crossref filters.
		related_feeds = Feed.objects.filter( filters__base__crossref=True,
			subscriber__site__subscriber__feed__in=related_feeds )
		# Pure performance-hack: find time threshold after which we just "don't care",
		#  since it's too old history and shouldn't be relevant anymore.
		# Value is set for FilterBase, so results should be recalculated in max-span delta.
		date_threshold = related_feeds\
			.values_list('filters__base__crossref_span', flat=True)
		try: next(it.dropwhile(bool, date_threshold))
		except StopIteration: date_threshold = max(date_threshold)
		else: date_threshold = None # there's at least one "reference-all" value
		if date_threshold:
			date_threshold = datetime.now() - timedelta(date_threshold)
			if rebuild_spec == FILTER_CR_REBUILD.new and isinstance(instance, dict):
				# date_threshold here is one of the timestamps (determined by rebuild_order)
				#  of the oldest post. This should affect each new/updated post's filtering result,
				#  as well as other feeds' results within the same timespan.
				date_threshold = max( date_threshold,
					min(it.imap(op.attrgetter(rebuild_order), affected_posts)) )
		# Set anti-recursion lock.
		Feed._filters_update_handler_lock = True
		# This actually drops all the results before "date_threshold" on "related_feeds".
		# Note that local update-date is checked, not the remote "date_modified" field.
		related_feeds = set(related_feeds) # so it won't generate repeated queries
		tainted = FilterResult.objects.filter(post__feed__in=related_feeds, filter__base__crossref=True)
		if date_threshold:
			tainted = tainted.filter(**{ 'post__{0}__gt'\
				.format(rebuild_order): date_threshold })
		tainted.delete()
		## Now, walk the posts, checking/updating results for each one.
		# Posts are be updated in the "last-touched" order, for consistency of cross-ref filters' results.
		# Amount of work here is quite extensive, since this (ideally) should affect every Post.
		tainted = Post.objects.filter(feed__in=related_feeds)
		if date_threshold: tainted = tainted.filter(**{'{0}__gt'.format(rebuild_order): date_threshold})
		for post in tainted.order_by(rebuild_order): post.filtering_result_update()
		## Unlock this function again.
		Feed._filters_update_handler_lock = False

	# Anti-recursion flag, class-global
	_filters_update_handler_lock = False

	@staticmethod
	def update_handler(feeds):
		'''Update all cross-referencing filters results for feeds and others, related to them.
			Intended to be called from non-Feed update hooks (like new Post saving).'''
		# Check if this call is a result of actions initiated from
		#  one of the hooks in a higher frame (resulting in recursion).
		if Feed._filters_update_handler_lock: return
		return Feed._filters_update_handler(Feed, feeds, force=True)

signals.m2m_changed.connect(Feed._filters_update_handler, sender=Feed.filters.through)

# These two are purely to handle filters_logic field updates
signals.pre_save.connect(Feed._filters_update_handler_check, sender=Feed)
signals.post_save.connect(Feed._filters_update_handler, sender=Feed)



class Tag(models.Model):
	name = models.CharField(_('name'), max_length=127, unique=True)

	class Meta:
		verbose_name = _('tag')
		verbose_name_plural = _('tags')
		ordering = ('name',)

	def __unicode__(self): return self.name




class PostQuerySet(models.query.QuerySet):
	# Limit on a string length (in bytes!) to match with levenshtein function
	# It's hardcoded in fuzzymatch.c as 255, by default, so this
	#  setting should not be higher than that or you'll get runtime errors.
	# My default is 1023 because fuzzymatch.c is patched as well ;)
	levenshtein_limit = 1023

	def similar(self, threshold, **criterias):
		'''Find text-based field matches with similarity (1-levenshtein/length)
			higher than specified threshold (0 to 1, 1 being an exact match)'''
		meta = self.model._meta
		funcs, params = list(), list()
		for name,val in criterias.iteritems():
			name = meta.get_field(name, many_to_many=False).column
			name = '.'.join(it.imap(connection.ops.quote_name, (meta.db_table, name)))
			# Alas, pg_trgm is for containment tests, not fuzzy matches,
			#  but it can potentially be used to find closest results as well
			# funcs.append( 'similarity(CAST({0}.{1} as text), CAST(%s as text))'\
			# Ok, these two are just to make sure levenshtein() won't crash
			#  w/ "argument exceeds the maximum length of N bytes error"
			funcs.append('octet_length({0}) <= {1}'.format(name, self.levenshtein_limit))
			funcs.append('octet_length(%s) <= {0}'.format(self.levenshtein_limit))
			# Then there's a possibility of division by zero...
			funcs.append('length({0}) > 0'.format(name))
			# And if everything else fits, the comparison itself
			funcs.append('levenshtein({0}, %s) / CAST(length({0}) AS numeric) < %s'.format(name))
			params.extend((val, val, float(1 - threshold)))
		return self.extra(where=funcs, params=params)

	def with_criterias(self, site, feed=None, tag=None, since=None):
		self = self.filter(feed__subscriber__site=site)
		if feed is not None: self = self.filter(feed=feed)
		if tag: self = self.filter(tags__name=tag)
		if since: self = self.filter(date_modified__gt=since)
		return self

	def sorted(self, site_ordering_id, force=None):
		prime = Post._get_ordering_attribute(site_ordering_id)
		if site_ordering_id == SITE_ORDERING.created_day:
			# Requires more handling than just raw attribute name
			self = self.extra(dict(
				date_created_day="date_trunc('day', {})".format(prime) ))
			prime = '-date_created_day'
		else: prime = '-{}'.format(prime)

		if force == 'asc': prime = prime.lstrip('-')
		elif force == 'desc' and prime[0] != '-': prime = '-{}'.format(prime)

		return self.order_by(prime, 'feed', '-date_created')


class Posts(models.Manager):
	def get_query_set(self): return PostQuerySet(self.model)

	@ft.wraps(PostQuerySet.similar)
	def similar(self, *argz, **kwz):
		return self.get_query_set().similar(*argz, **kwz)

	def filtered(self, site=None, for_display=True, **criterias):
		# Check is "not False" because there can be NULLs for
		#  feeds with no filters (also provided there never was any filters).
		# TODO: make this field pure-bool?
		posts = self.get_query_set().exclude(filtering_result=False)
		if for_display:
			posts = posts.exclude(hidden=True)
			posts = posts.filter(feed__subscriber__is_active=True)
		return posts.with_criterias(site, **criterias) if site else posts


class Post(models.Model):
	objects = Posts()

	feed = models.ForeignKey(Feed, verbose_name=_('feed'), related_name='posts')
	title = models.CharField(_('title'), max_length=2047)
	link = models.URLField(_('link'), max_length=2047) # look at hashify.me for reasoning behind 2k+ length
	content = models.TextField(_('content'), blank=True)
	date_modified = models.DateTimeField(_('date modified'), null=True, blank=True)
	guid = models.CharField(_('guid'), max_length=511, db_index=True)
	author = models.CharField(_('author'), max_length=255, blank=True)
	author_email = models.EmailField(_('author email'), blank=True)
	comments = models.URLField(_('comments'), max_length=511, blank=True)
	tags = models.ManyToManyField(Tag, verbose_name=_('tags'), blank=True)
	hidden = models.BooleanField( default=False,
		help_text='Manual switch to completely hide the Post,'
			' although it will be present for internal checks, like filters.' )

	# These two will be quite different from date_modified, since date_modified is
	#  parsed from the feed itself, and should always be earlier than either of two
	date_created = models.DateTimeField(_('date created'), auto_now_add=True)
	date_updated = models.DateTimeField(_('date updated'), auto_now=True)

	# This one is an aggregate of filtering_results, for performance benefit
	filtering_result = models.NullBooleanField()
	# filtering_results (reverse fk from FilterResult)

	class Meta:
		verbose_name = _('post')
		verbose_name_plural = _('posts')
		ordering = ('-date_modified',)
		unique_together = (('feed', 'guid'),)


	@staticmethod
	def _get_ordering_attribute(site_ordering_id):
		# Abstracts SITE_ORDERING/Post relationship somewhat,
		#  but created_day requires special handling while sorting entries anyway
		if site_ordering_id == SITE_ORDERING.modified: return 'date_modified'
		elif site_ordering_id == SITE_ORDERING.created: return 'date_created'
		elif site_ordering_id == SITE_ORDERING.created_day: return 'date_created'
		else: raise ValueError('Unknown ordering method id: {0}'.format(site_ordering_id))

	def date_on_site(self, site):
		return getattr(self, self._get_ordering_attribute(site.order_posts_by))


	def _filtering_result(self, by_or):
		return self.filtering_results.filter(
			result=bool(by_or) )[0].result # find at least one failed / passed test

	def _filtering_result_checked(self, by_or):
		'''Check if post passes all / at_least_one (by_or parameter) filter(s).
			Filters are evaluated on only-if-necessary ("lazy") basis.'''
		filters, results = it.imap(set, ( self.feed.filters.all(),
			self.filtering_results.values_list('filter', flat=True) ))

		# Check if conclusion can already be made, based on cached results.
		if results.issubset(filters):
			# If at least one failed/passed test is already there, and/or outcome is defined.
			try: return self._filtering_result(by_or)
			except IndexError: # inconclusive until results are consistent
				if filters == results: return not by_or

		# Consistency check / update.
		if filters != results:
			# Drop obsolete (removed, unbound from feed)
			#  filters' results (they WILL corrupt outcome).
			self.filtering_results.filter(filter__in=results.difference(filters)).delete()
			# One more try, now that results are only from feed filters' subset.
			try: return self._filtering_result(by_or)
			except IndexError: pass
			# Check if any filter-results are not cached yet, create them (perform actual filtering).
			# Note that independent filters applied first, since
			#  crossrefs should be more resource-hungry in general.
			for filter_obj in sorted(filters.difference(results), key=op.attrgetter('base.crossref')):
				filter_op = FilterResult(filter=filter_obj, post=self, result=filter_obj.handler(self))
				filter_op.save()
				if filter_op.result == by_or: return by_or # return as soon as first passed / failed

		# Final result
		try: return self._filtering_result(by_or)
		except IndexError: return not by_or # none passed / none failed

	def filtering_result_update(self):
		filtering_result = self._filtering_result_checked(
			by_or=(self.feed.filters_logic == FEED_FILTERING_LOGIC.any) )
		if self.filtering_result != filtering_result:
			self.filtering_result = filtering_result
			self.save()


	def __unicode__(self): return self.title
	def get_absolute_url(self): return self.link


	@staticmethod
	def _update_handler(sender, instance, delete=False, **kwz):
		if transaction_in_progress.is_set():
			# In case of post_delete hook, added object is not in db anymore
			transaction_affected_feeds[instance.feed].add(instance)
		elif not instance._update_handler_call:
			instance._update_handler_call = True
			try:
				Feed.update_handler( {instance.feed: [instance]}
					if not delete else [instance.feed] ) # so handler won't try to recalculate filtering for it
			finally: instance._update_handler_call = False
	_update_handler_call = False # flag to avoid recursion in filtering_result_update

signals.post_save.connect(Post._update_handler, sender=Post)
signals.post_delete.connect(ft.partial(Post._update_handler, delete=True), sender=Post)




class Subscriber(models.Model):
	site = models.ForeignKey(Site, verbose_name=_('site'))
	feed = models.ForeignKey(Feed, verbose_name=_('feed'))

	name = models.CharField(_('name'), max_length=100, null=True, blank=True,
		help_text=_('Keep blank to use the Feed\'s original name.') )
	shortname = models.CharField( _('shortname'), max_length=50, null=True,
		blank=True, help_text=_('Keep blank to use the Feed\'s original shortname.') )
	is_active = models.BooleanField( _('is active'), default=True,
		help_text=_('If disabled, this subscriber will not appear in the site or in the site\'s feed.') )

	class Meta:
		verbose_name = _('subscriber')
		verbose_name_plural = _('subscribers')
		ordering = ('site', 'name', 'feed')
		unique_together = (('site', 'feed'),)

	def __unicode__(self): return u'%s in %s' % (self.feed, self.site)

	def get_cloud(self):
		from feedjack import fjcloud
		return fjcloud.getcloud(self.site, self.feed.id)

	def save(self):
		if not self.name: self.name = self.feed.name
		if not self.shortname: self.shortname = self.feed.shortname
		super(Subscriber, self).save()


	@staticmethod
	def _update_handler_check(sender, instance, **kwz):
		try: pre_instance = Subscriber.objects.get(id=instance.id)
		except ObjectDoesNotExist: pass # just created
		else:
			instance._relation_update = (
				instance.site != pre_instance.site
					or instance.feed != pre_instance.feed )
	_relation_update = None

	@staticmethod
	def _update_handler(sender, instance, created, **kwz):
		if created: return
		if instance._relation_update: Feed.update_handler(instance.feed)

signals.pre_save.connect(Subscriber._update_handler_check, sender=Subscriber)
signals.post_save.connect(Subscriber._update_handler, sender=Subscriber)




from django.db import transaction, IntegrityError
from django.dispatch import Signal

# Following signals are wired into django by monkey-patching,
#  because there's no support for these in 1.2.
# See also: http://code.djangoproject.com/ticket/14051

transaction_pre_commit = Signal(providing_args=list())
transaction_post_commit = Signal(providing_args=list())
transaction_pre_rollback = Signal(providing_args=list())
transaction_post_rollback = Signal(providing_args=list())

_django_commit = transaction.commit
@ft.wraps(transaction.commit)
def signaled_commit(using=None):
	transaction_pre_commit.send(sender=using)
	_django_commit(using=using)
	transaction_post_commit.send(sender=using)
transaction.commit = signaled_commit

_django_rollback = transaction.rollback
@ft.wraps(transaction.rollback)
def signaled_rollback(using=None):
	transaction_pre_rollback.send(sender=using)
	_django_rollback(using=using)
	transaction_post_rollback.send(sender=using)
transaction.rollback = signaled_rollback


# These are sent along with transaction_wrapper func start/finish
transaction_start = Signal(providing_args=list())
transaction_finish = Signal(providing_args=['error']) # error holds exception or None

def transaction_wrapper(func, logger=None):
	'''Traps exceptions in transaction.commit_manually blocks,
		instead of just replacing them by non-meaningful no-commit django exceptions'''
	if (func is not None and logger is not None)\
			or not (isinstance(func, logging.Logger) or func is logging):
		@transaction.commit_manually
		@ft.wraps(func)
		def _transaction_wrapper(*argz, **kwz):
			transaction_start.send(sender=func.func_name)
			try: result = func(*argz, **kwz)
			except Exception as err:
				transaction_finish.send(sender=func.func_name, error=err)
				import sys, traceback
				(logger or log).error(( u'Unhandled exception: {0},'
					' traceback:\n {1}' ).format( err,
						smart_unicode(''.join(traceback.format_tb(sys.exc_info()[2]))) ))
				raise
			else:
				transaction_finish.send(sender=func.func_name, error=None)
			return result
		return _transaction_wrapper
	else:
		return ft.partial(transaction_wrapper, logger=func)


# These are here to defer costly FilterResult updates until the end of transaction,
#  so they won't be dropped-recalculated for every new or updated Post.
# Note that this is only for Post-hooks, any relation changes
#  (Feed-Filter, Subscriber, etc) should still trigger an immediate rebuild.

from threading import Event
transaction_in_progress = Event()
transaction_affected_feeds = defaultdict(set)

def transaction_bulk_start(signal, sender, **kwz):
	transaction_in_progress.set()

def transaction_bulk_process(signal, sender, **kwz):
	if not transaction_in_progress.is_set(): return
	Feed.update_handler(transaction_affected_feeds)
	transaction_affected_feeds.clear() # in case of several commits

def transaction_bulk_cancel(signal, sender, **kwz):
	if transaction_in_progress.is_set(): return
	transaction_affected_feeds.clear() # not to interfere with next commit

def transaction_bulk_finish(signal, sender, **kwz):
	# Transaction should be already comitted/rolled-back at this point
	transaction_in_progress.clear()
	transaction_affected_feeds.clear()

transaction_start.connect(transaction_bulk_start, sender='bulk_update')
transaction_pre_commit.connect(transaction_bulk_process)
transaction_post_rollback.connect(transaction_bulk_cancel)
transaction_finish.connect(transaction_bulk_finish, sender='bulk_update')
