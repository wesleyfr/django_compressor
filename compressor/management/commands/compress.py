import os
import sys
import warnings
from fnmatch import fnmatch
from optparse import make_option

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

from django.conf import settings as django_settings
from django.core.management.base import  NoArgsCommand, CommandError
from django.template import Context, Template, TemplateDoesNotExist, TemplateSyntaxError
from django.utils.datastructures import SortedDict

from compressor.cache import cache
from compressor.conf import settings
from compressor.exceptions import OfflineGenerationError
from compressor.templatetags.compress import CompressorNode
from compressor.utils import get_offline_cachekey, walk, any, import_module


class Command(NoArgsCommand):
    help = "Generate the compressor content outside of the request/response cycle"
    option_list = NoArgsCommand.option_list + (
        make_option('--extension', '-e', action='append', dest='extensions',
            help='The file extension(s) to examine (default: ".html", '
                'separate multiple extensions with commas, or use -e '
                'multiple times)'),
        make_option('-f', '--force', default=False, action='store_true', dest='force',
            help="Force generation of compressor content even if "
                "COMPRESS setting is not True."),
        make_option('--follow-links', default=False, action='store_true', dest='follow_links',
            help="Follow symlinks when traversing the COMPRESS_ROOT "
                "(which defaults to MEDIA_ROOT). Be aware that using this "
                "can lead to infinite recursion if a link points to a parent "
                "directory of itself."),
    )
    def get_loaders(self):
        from django.template.loader import template_source_loaders
        if template_source_loaders is None:
            try:
                from django.template.loader import find_template as finder_func
            except ImportError:
                from django.template.loader import find_template_source as finder_func
            try:
                source, name = finder_func('test')
            except TemplateDoesNotExist, e:
                pass
            from django.template.loader import template_source_loaders
        return template_source_loaders or []

    def compress(self, log=None, **options):
        """
        Searches templates containing 'compress' nodes and compresses them
        "offline" -- outside of the request/response cycle.

        The result is cached with a cache-key derived from the content of the
        compress nodes (not the content of the possibly linked files!).
        """
        extensions = options.get('extensions')
        extensions = self.handle_extensions(extensions or ['html'])
        verbosity = int(options.get("verbosity", 0))
        if not log:
            log = StringIO()
        if not django_settings.TEMPLATE_LOADERS:
            raise OfflineGenerationError("No template loaders defined. You "
                                         "must set TEMPLATE_LOADERS in your "
                                         "settings.")
        paths = set()
        for loader in self.get_loaders():
            try:
                module = import_module(loader.__module__)
                get_template_sources = getattr(module, 'get_template_sources', None)
                if get_template_sources is None:
                    get_template_sources = loader.get_template_sources
                paths.update(list(get_template_sources('')))
            except (ImportError, AttributeError), e:
                # Yeah, this didn't work out so well, let's move on
                pass
        if not paths:
            raise OfflineGenerationError("No template paths found. None of "
                                         "the configured template loaders "
                                         "provided template paths. See "
                                         "http://django.me/template-loaders "
                                         "for more information on template "
                                         "loaders.")
        if verbosity > 1:
            log.write("Considering paths:\n\t" + "\n\t".join(paths) + "\n")
        templates = set()
        for path in paths:
            for root, dirs, files in walk(path, followlinks=options.get('followlinks', False)):
                templates.update(os.path.join(root, name)
                    for name in files if any(fnmatch(name, "*%s" % glob)
                        for glob in extensions))
        if not templates:
            raise OfflineGenerationError("No templates found. Make sure your "
                                         "TEMPLATE_LOADERS and TEMPLATE_DIRS "
                                         "settings are correct.")
        if verbosity > 1:
            log.write("Found templates:\n\t" + "\n\t".join(templates) + "\n")

        compressor_nodes = SortedDict()
        for template_name in templates:
            try:
                template_file = open(template_name)
                try:
                    template = Template(template_file.read().decode(
                                        django_settings.FILE_CHARSET))
                finally:
                    template_file.close()
            except IOError: # unreadable file -> ignore
                if verbosity > 0:
                    log.write("Unreadable template at: %s\n" % template_name)
                continue
            except TemplateSyntaxError: # broken template -> ignore
                if verbosity > 0:
                    log.write("Invalid template at: %s\n" % template_name)
                continue
            except UnicodeDecodeError:
                if verbosity > 0:
                    log.write("UnicodeDecodeError while trying to read "
                              "template %s\n" % template_name)
            nodes = list(self.walk_nodes(template))
            if nodes:
                compressor_nodes.setdefault(template_name, []).extend(nodes)

        if not compressor_nodes:
            raise OfflineGenerationError("No 'compress' template tags found in templates.")

        if verbosity > 0:
            log.write("Found 'compress' tags in:\n\t" +
                      "\n\t".join(compressor_nodes.keys()) + "\n")

        log.write("Compressing... ")
        count = 0
        results = []
        context = Context(settings.OFFLINE_CONTEXT)
        for nodes in compressor_nodes.values():
            for node in nodes:
                key = get_offline_cachekey(node.nodelist)
                result = node.render(context, compress=True, offline=False)
                cache.set(key, result, settings.OFFLINE_TIMEOUT)
                results.append(result)
                count += 1
        log.write("done\nCompressed %d block(s) from %d template(s).\n"
                  % (count, len(compressor_nodes)))
        return count, results

    def walk_nodes(self, node):
        for node in getattr(node, "nodelist", []):
            if (isinstance(node, CompressorNode) or
                    node.__class__.__name__ == "CompressorNode"): # for 1.1.X
                yield node
            else:
                for node in self.walk_nodes(node):
                    yield node

    def handle_extensions(self, extensions=('html',)):
        """
        organizes multiple extensions that are separated with commas or
        passed by using --extension/-e multiple times.

        for example: running 'django-admin compress -e js,txt -e xhtml -a'
        would result in a extension list: ['.js', '.txt', '.xhtml']

        >>> handle_extensions(['.html', 'html,js,py,py,py,.py', 'py,.py'])
        ['.html', '.js']
        >>> handle_extensions(['.html, txt,.tpl'])
        ['.html', '.tpl', '.txt']
        """
        ext_list = []
        for ext in extensions:
            ext_list.extend(ext.replace(' ','').split(','))
        for i, ext in enumerate(ext_list):
            if not ext.startswith('.'):
                ext_list[i] = '.%s' % ext_list[i]

        # we don't want *.py files here because of the way non-*.py files
        # are handled in make_messages() (they are copied to file.ext.py files to
        # trick xgettext to parse them as Python files)
        return set([x for x in ext_list if x != '.py'])

    def handle_noargs(self, **options):
        if not settings.COMPRESS and not options.get("force"):
            raise CommandError("Compressor is disabled. Set COMPRESS "
                               "settting to True to enable it "
                               "(Use -f/--force to override).")
        if not settings.OFFLINE:
            if not options.get("force"):
                raise CommandError("Aborting; COMPRESS_OFFLINE is not set. "
                                   "(Use -f/--force to override)")
            warnings.warn("COMPRESS_OFFLINE is not set. Offline generated "
                          "cache will not be used.")
        self.compress(sys.stdout, **options)
