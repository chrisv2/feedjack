# -*- coding: utf-8 -*-

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import signals
from django.db import models, connection
from django.utils.translation import ugettext_lazy as _
from django.utils.encoding import smart_unicode

from feedjack import fjcache, filters

import itertools as it, operator as op, functools as ft
from collections import namedtuple
from datetime import datetime, timedelta



class Link(models.Model):
	name = models.CharField(_('name'), max_length=100, unique=True)
	link = models.URLField(_('link'), verify_exists=True)

	class Meta:
		verbose_name = _('link')
		verbose_name_plural = _('links')

	class Admin: pass

	def __unicode__(self): return u'%s (%s)' % (self.name, self.link)



class Site(models.Model):
	name = models.CharField(_('name'), max_length=100)
	url = models.CharField(_('url'),
	  max_length=100,
	  unique=True,
	  help_text=u'%s: %s, %s' % (smart_unicode(_('Example')),
		u'http://www.planetexample.com',
		u'http://www.planetexample.com:8000/foo'))
	title = models.CharField(_('title'), max_length=200)
	description = models.TextField(_('description'))
	welcome = models.TextField(_('welcome'), null=True, blank=True)
	greets = models.TextField(_('greets'), null=True, blank=True)

	default_site = models.BooleanField(_('default site'), default=False)
	posts_per_page = models.PositiveIntegerField(_('posts per page'), default=20)
	order_posts_by = models.PositiveSmallIntegerField(_('order posts by'), default=1, choices=(
		(1, _('Date published.')), (2, _('Date the post was first obtained.')) ))
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



class FilterBase(models.Model): # I had to resist the urge to call it FilterClass or FilterModel

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
			' (and possibly their filtering results) or not.' )
	crossref_span = models.PositiveSmallIntegerField( 'How many days of history'
		' should be re-referenced on post changes to keep this results conclusive.'
		' Performance-quality knob, since ideally this should be an infinity.' )

	@property
	def handler(self):
		'Handler function'
		filter_func = getattr(filters, self.handler_name or self.name, None)
		if filter_func is None:
			if '.' not in self.handler_name:
				raise ImportError('Filter function not found: {0}'.format(self.handler_name))
			filter_module, filter_func = it.imap(str, self.handler_name.rsplit('.', 1))
			filter_func = getattr(__import__(filter_module, fromlist=[filter_func]), filter_func)
		return filter_func

	def __unicode__(self): return u'{0.name} ({0.handler_name})'.format(self)


class Filter(models.Model):
	base = models.ForeignKey('FilterBase', related_name='filters')
	# feeds (reverse m2m relation from Feed)
	parameter = models.CharField( max_length=512, blank=True, null=True,
		help_text='Parameter keyword to pass to a filter function.<br />Allows to define generic'
			' filtering alghorithms in code (like "regex_filter") and actual filters in db itself'
			' (specifying regex to filter by).<br />Null value would mean that "parameter" keyword'
			' wont be passed to handler at all.' )

	@property
	def handler(self):
		'Parametrized handler function'
		return ft.partial(self.base.handler, parameter=self.parameter)\
			if self.parameter is not None else self.base.handler

	@property
	def shortname(self): return self.__unicode__(short=True)
	def __unicode__(self, short=False):
		usage = [self.parameter] if self.parameter else list()
		if not short:
			binding = u', '.join(it.imap(op.attrgetter('shortname'), self.feeds.all()))
			usage.append(u'used on {0}'.format(binding) if binding else 'not used for any feed')
		return u'{0.base.name}{1}'.format(self, u' ({0})'.format(u', '.join(usage) if usage else ''))


class FilterResult(models.Model):
	filter = models.ForeignKey('Filter')
	post = models.ForeignKey('Post', related_name='filtering_results')
	result = models.BooleanField()
	timestamp = models.DateTimeField(auto_now=True)

	def __unicode__(self):
		return u'{0.result} ("{0.post}", {0.filter.shortname} on'\
			u' {0.post.feed.shortname}, {0.timestamp})'.format(self)



FEED_FILTERING_LOGIC = namedtuple('FilterLogic', 'all any')(*xrange(2))

class Feed(models.Model):
	feed_url = models.URLField(_('feed url'), unique=True)

	name = models.CharField(_('name'), max_length=100)
	shortname = models.CharField(_('shortname'), max_length=50)
	immutable = models.BooleanField( _('immutable'), default=False,
		help_text=_('Do not update posts that were already fetched.') )
	is_active = models.BooleanField( _('is active'), default=True,
		help_text=_('If disabled, this feed will not be further updated.') )

	title = models.CharField(_('title'), max_length=200, blank=True)
	tagline = models.TextField(_('tagline'), blank=True)
	link = models.URLField(_('link'), blank=True)

	filters = models.ManyToManyField('Filter', related_name='feeds')
	filters_logic = models.PositiveSmallIntegerField('Composition', choices=(
		(FEED_FILTERING_LOGIC.all, 'Should pass ALL filters (AND logic)'),
		(FEED_FILTERING_LOGIC.any, 'Should pass ANY of the filters (OR logic)') ))

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
			instance._filters_logic_update = ( instance.filters_logic\
				!= Feed.objects.get(id=instance.id).filters_logic )
		except ObjectDoesNotExist: pass # shouldn't really matter
	_filters_logic_update = None

	@staticmethod
	def _filters_update_handler( sender, instance, force=False,
			created=None, reverse=None, model=None, pk_set=list(), **kwz ):
		### Main "crossref-rebuild" function. ALL filter-consistency hooks call it in the end.
		### Logic here is pretty obscure, so I'll try to explain it in comments.
		## Check if this call is a result of actions initiated from
		##  this very function in a higher frame (recursion).
		if Feed._filters_update_handler_lock: return
		## post_save-specific checks, so it won't be triggered on _every_
		##  Feed save, only those that change "filter_logic" on existing feeds.
		if not force and created is not None and (
			created is True or not instance._filters_logic_update ): return
		## Set anti-recursion lock.
		Feed._filters_update_handler_lock = True
		## Get set of feeds that are affected by m2m update, note that it's always just
		##  [instance] in case of post_save hook, since it doesn't pass "reverse" keyword.
		related_feeds = [instance] if not reverse else Feed.objects.filter(id__in=pk_set)
		## Get all Sites, incorporating the feed (all their feeds are affected), then
		## drop cross-referencing filters' results, as they'd be totally screwed, note that
		##  this means dropping all such results for every feed that shares a Site with "instance".
		# This is a set of feeds that share the Site(s) with "instance" _and_ have crossref filters.
		related_feeds = Feed.objects.filter( filters__base__crossref=True,
			subscriber_set__site__subscriber_set__feed__in=related_feeds )
		# Pure performance-hack: find time threshold after which we just "don't care",
		#  since it's too old history and shouldn't be relevant anymore.
		# Value is set for FilterBase, so results should be recalculated in max-span delta.
		date_threshold, = related_feeds.aggregate(
			models.Max(models.F('base__crossref_span')) ).itervalues()
		# This actually drops all the results before "date_threshold" on "related_feeds"
		FilterResult.objects.filter( post__feed__in=related_feeds,
			post__date_created__gt=date_threshold, filter__base__crossref==True ).delete()
		## Now, walk the posts, checking/updating results for each one.
		# Posts should be updated in the "added" order, for consistency of cross-ref filters' results.
		# Amount of work here is quite extensive, since this (ideally) should affect every Post.
		for post in Post.objects.filter( feed__in=related_feeds,
			date_created__gt=date_threshold ).order_by('date_created'): post.filtering_result_update()
		# Special case: updated filtering logic, that certainly affects every post of "instance",
		#  so they all should be updated. Shouldn't happen too often anyway.
		if reverse is not None or (created is False and instance._filters_logic_update):
			for post in instance.posts.order_by('date_created'): post.filtering_result_update()
		## Unlock this function again.
		Feed._filters_update_handler_lock = False

	# Anti-recursion flag, class-global
	_filters_update_handler_lock = False

	def posts_update_handler(self):
		'Update all cross-referencing filters results for this and related feeds'
		return self._filters_update_handler(self.__class__, self, force=True)

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
	def similar(self, threshold, **criterias):
		'''Find text-based field matches with similarity (1-levenshtein/length)
			higher than specified threshold (0 to 1, 1 being an exact match)'''
		meta = self.model._meta
		funcs, params = list(), list()
		for name,val in criterias.iteritems():
			name = meta.get_field(name, many_to_many=False).column
			name = '.'.join(it.imap(connection.ops.quote_name, (meta.db_table, name)))
			# Alas, pg_trgm is for containment tests, not fuzzy matches ;(
			# funcs.append( 'similarity(CAST({0}.{1} as text), CAST(%s as text))'\
			funcs.append('length({0}) > 0'.format(name)) # either that or div_by_zero crash
			funcs.append('levenshtein({0}, %s) / CAST(length({0}) AS numeric) < %s'.format(name))
			params.extend((val, float(1 - threshold)))
		return self.extra(where=funcs, params=params)

class Posts(models.Manager):
	def get_query_set(self): return PostQuerySet(self.model)

	@ft.wraps(PostQuerySet.similar)
	def similar(self, *argz, **kwz):
		return self.get_query_set().similar(*argz, **kwz)
	def filtered(self):
		return self.get_query_set().filter(filtering_result=True)


class Post(models.Model):
	objects = Posts()

	feed = models.ForeignKey(Feed, verbose_name=_('feed'), related_name='posts')
	title = models.CharField(_('title'), max_length=511)
	link = models.URLField(_('link'), max_length=511)
	content = models.TextField(_('content'), blank=True)
	date_modified = models.DateTimeField(_('date modified'), null=True, blank=True)
	guid = models.CharField(_('guid'), max_length=511, db_index=True)
	author = models.CharField(_('author'), max_length=255, blank=True)
	author_email = models.EmailField(_('author email'), blank=True)
	comments = models.URLField(_('comments'), max_length=511, blank=True)
	tags = models.ManyToManyField(Tag, verbose_name=_('tags'))
	date_created = models.DateField(_('date created'), auto_now_add=True)

	filtering_result = models.NullBooleanField()
	# filtering_results (reverse fk from FilterResult)

	class Meta:
		verbose_name = _('post')
		verbose_name_plural = _('posts')
		ordering = ('-date_modified',)
		unique_together = (('feed', 'guid'),)


	def _filtering_result(self, by_or):
		return self.filtering_results.filter(
			result=bool(by_or) )[0].result # find at least one failed / passed test

	def _filtering_result_checked(self, by_or):
		'''Check if post passes all / at_least_one (by_or parameter) filter(s).
			Filters are evaluated on only-if-necessary ("lazy") basis.'''
		filters, results = it.imap(set, ( self.feed.filters.all(),
			it.imap(op.attrgetter('filter'), self.filtering_results.all()) ))

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
	def _update_handler(sender, instance, **kwz):
		if not instance._update_handler_call:
			instance._update_handler_call = True
			try: instance.feed.posts_update_handler()
			finally: instance._update_handler_call = False
	_update_handler_call = False # flag to avoid recursion in filtering_result_update

signals.post_save.connect(Post._update_handler, sender=Post)
signals.post_delete.connect(Post._update_handler, sender=Post)




class Subscriber(models.Model):
	site = models.ForeignKey(Site, verbose_name=_('site') )
	feed = models.ForeignKey(Feed, verbose_name=_('feed') )

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
			self._relation_update = ( instance.site != pre_instance.site
				or instance.feed != pre_instance.feed )
	_relation_update = None

	@staticmethod
	def _update_handler(sender, instance, created, **kwz):
		if created: return
		if self._relation_update: instance.feed.posts_update_handler()

signals.pre_save.connect(Subscriber._update_handler_check, sender=Subscriber)
signals.post_save.connect(Subscriber._update_handler, sender=Subscriber)
