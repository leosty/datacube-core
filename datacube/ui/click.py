# coding=utf-8
"""
Common functions for click-based cli scripts.
"""
from __future__ import absolute_import

import functools
import logging
import os
import copy
import sys

import click
from click._compat import filename_to_ui

from datacube import config, __version__
from datacube.api.core import Datacube
from datacube.config import LocalConfig

from datacube.executor import get_executor, mk_celery_executor

from pathlib import Path
from datacube.drivers.manager import DriverManager

from datacube.ui.expression import parse_expressions
from sqlalchemy.exc import OperationalError, ProgrammingError

_LOG_FORMAT_STRING = '%(asctime)s %(process)d %(name)s %(levelname)s %(message)s'
CLICK_SETTINGS = dict(help_option_names=['-h', '--help'])
_LOG = logging.getLogger(__name__)


def _print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return

    click.echo(
        '{prog}, version {version}'.format(
            prog='Open Data Cube core',
            version=__version__
        )
    )
    ctx.exit()


def compose(*functions):
    """
    >>> compose(
    ...     lambda x: x+1,
    ...     lambda y: y+2
    ... )(1)
    4
    """

    def compose2(f, g):
        return lambda x: f(g(x))

    return functools.reduce(compose2, functions, lambda x: x)


class ColorFormatter(logging.Formatter):
    colors = {
        'info': dict(fg='white'),
        'error': dict(fg='red'),
        'exception': dict(fg='red'),
        'critical': dict(fg='red'),
        'debug': dict(fg='blue'),
        'warning': dict(fg='yellow')
    }

    def format(self, record):
        if not record.exc_info:
            record = copy.copy(record)
            record.levelname = click.style(record.levelname, **self.colors.get(record.levelname.lower(), {}))
        return logging.Formatter.format(self, record)


class ClickHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            click.echo(msg, err=True)
        except (KeyboardInterrupt, SystemExit):
            raise
        except:  # pylint: disable=bare-except
            self.handleError(record)


def _init_logging(ctx, param, value):
    handler = ClickHandler()
    handler.formatter = ColorFormatter(_LOG_FORMAT_STRING)
    logging.root.addHandler(handler)

    logging_level = logging.WARN - 10 * value
    logging.root.setLevel(logging_level)
    logging.getLogger('datacube').setLevel(logging_level)

    logging.getLogger('datacube').info('Running datacube command: %s', ' '.join(sys.argv))
    if logging_level <= logging.INFO:
        logging.getLogger('rasterio').setLevel(logging.INFO)

    if not ctx.obj:
        ctx.obj = {}

    ctx.obj['verbosity'] = value


def _add_logfile(ctx, param, value):
    formatter = logging.Formatter(_LOG_FORMAT_STRING)
    for logfile in value:
        handler = logging.FileHandler(logfile)
        handler.formatter = formatter
        logging.root.addHandler(handler)


def _log_queries(ctx, param, value):
    if value:
        logging.getLogger('sqlalchemy.engine').setLevel('INFO')


def _set_config(ctx, param, value):
    if value:
        if not any(os.path.exists(p) for p in value):
            raise ValueError('No specified config paths exist: {}' % value)

        if not ctx.obj:
            ctx.obj = {}
        paths = value
        ctx.obj['config_files'] = paths


def _set_driver(ctx, param, value):
    if not ctx.obj:
        ctx.obj = {}
    ctx.obj['driver'] = value


def _set_environment(ctx, param, value):
    if not ctx.obj:
        ctx.obj = {}
    ctx.obj['config_environment'] = value


#: pylint: disable=invalid-name
version_option = click.option('--version', is_flag=True, callback=_print_version,
                              expose_value=False, is_eager=True)
#: pylint: disable=invalid-name
verbose_option = click.option('--verbose', '-v', count=True, callback=_init_logging,
                              is_eager=True, expose_value=False, help="Use multiple times for more verbosity")
#: pylint: disable=invalid-name
logfile_option = click.option('--log-file', multiple=True, callback=_add_logfile,
                              is_eager=True, expose_value=False, help="Specify log file")
#: pylint: disable=invalid-name
config_option = click.option('--config_file', '-C', multiple=True, default='', callback=_set_config,
                             expose_value=False)

#: pylint: disable=invalid-name
environment_option = click.option('--env', '-E', callback=_set_environment,
                                  expose_value=False)

#: pylint: disable=invalid-name
driver_option = click.option('--driver', '-D', default=None, callback=_set_driver,
                             expose_value=False)
#: pylint: disable=invalid-name
log_queries_option = click.option('--log-queries', is_flag=True, callback=_log_queries,
                                  expose_value=False, help="Print database queries.")

# This is a function, so it's valid to be lowercase.
#: pylint: disable=invalid-name
global_cli_options = compose(
    version_option,
    verbose_option,
    logfile_option,
    driver_option,
    environment_option,
    config_option,
    log_queries_option
)


@click.group(help="Data Cube command-line interface", context_settings=CLICK_SETTINGS)
@global_cli_options
def cli():
    pass


def pass_config(f):
    """Get a datacube config as the first argument. """

    def new_func(*args, **kwargs):
        obj = click.get_current_context().obj

        paths = obj.get('config_files') or config.DEFAULT_CONF_PATHS
        # If the user is overriding the defaults
        specific_environment = obj.get('config_environment')
        specific_driver = obj.get('driver')

        parsed_config = config.LocalConfig.find(paths=paths, env=specific_environment, driver=specific_driver)
        _LOG.debug("Loaded datacube config: %r", parsed_config)
        return f(parsed_config, *args, **kwargs)

    return functools.update_wrapper(new_func, f)


def pass_driver_manager(app_name=None, expect_initialised=True):
    """Get a driver manager as the first argument.

    :param str app_name:
        A short name of the application for logging purposes.
    :param bool expect_initialised:
        Whether to connect immediately on startup. Useful to catch connection config issues immediately,
        but if you're planning to fork before any usage (such as in the case of some web servers),
        you may not want this. For more information on thread/process usage, see datacube.index._api.Index
    """

    def decorate(f):
        @pass_config
        def with_driver_manager(local_config,  # type: LocalConfig
                                *args,
                                **kwargs):
            ctx = click.get_current_context()
            try:
                with DriverManager(index=None,
                                   default_driver_name=local_config.default_driver,
                                   local_config=local_config,
                                   application_name=app_name or ctx.command_path,
                                   validate_connection=expect_initialised) as driver_manager:
                    ctx.obj['index'] = driver_manager.index
                    _LOG.debug("Driver manager ready. Connected to index: %s",
                               driver_manager.index)
                    return f(driver_manager, *args, **kwargs)
            except (OperationalError, ProgrammingError) as e:
                handle_exception('Error Connecting to database: %s', e)

        return functools.update_wrapper(with_driver_manager, f)

    return decorate


def pass_index(app_name=None, expect_initialised=True):
    """
    Get an index from the current or default local settings.

    :param str app_name:
        A short name of the application for logging purposes.
    :param bool expect_initialised:
        Whether to connect immediately on startup. Useful to catch connection config issues immediately,
        but if you're planning to fork before any usage (such as in the case of some web servers),
        you may not want this. For More information on thread/process usage, see datacube.index._api.Index
    """

    def decorate(f):
        @pass_driver_manager(app_name=app_name, expect_initialised=expect_initialised)
        def with_index(driver_manager, *args, **kwargs):
            return f(driver_manager.index, *args, **kwargs)

        return functools.update_wrapper(with_index, f)

    return decorate


def pass_datacube(app_name=None, expect_initialised=True):
    """
    Get a DataCube from the current or default local settings.

    :param str app_name:
        A short name of the application for logging purposes.
    :param bool expect_initialised:
        Whether to connect immediately on startup. Useful to catch connection config issues immediately,
        but if you're planning to fork before any usage (such as in the case of some web servers),
        you may not want this. For More information on thread/process usage, see datacube.index._api.Index
    """

    def decorate(f):
        @pass_driver_manager(app_name=app_name, expect_initialised=expect_initialised)
        def with_index(driver_manager, *args, **kwargs):
            return f(Datacube(driver_manager=driver_manager), *args, **kwargs)

        return functools.update_wrapper(with_index, f)

    return decorate


def parse_endpoint(value):
    ip, port = tuple(value.split(':'))
    return ip, int(port)


EXECUTOR_TYPES = {
    'serial': lambda _: get_executor(None, None),
    'multiproc': lambda workers: get_executor(None, int(workers)),
    'distributed': lambda addr: get_executor(parse_endpoint(addr), True),
    'celery': lambda addr: mk_celery_executor(*parse_endpoint(addr))
}

EXECUTOR_TYPES['dask'] = EXECUTOR_TYPES['distributed']  # Add alias "dask" for distributed


def _setup_executor(ctx, param, value):
    try:
        return EXECUTOR_TYPES[value[0]](value[1])
    except ValueError:
        ctx.fail("Failed to create '%s' executor with '%s'" % value)


executor_cli_options = click.option('--executor',
                                    type=(click.Choice(EXECUTOR_TYPES.keys()), str),
                                    default=('serial', None),
                                    help="Run parallelized, either locally or distributed. eg:\n"
                                         "--executor multiproc 4 (OR)\n"
                                         "--executor distributed 10.0.0.8:8888",
                                    callback=_setup_executor)


def handle_exception(msg, e):
    """
    Exit following an exception in a CLI app

    If verbosity (-v flag) specified, dump out a stack trace. Otherwise,
    simply print the given error message.

    Include a '%s' in the message to print the single line message from the
    exception.

    :param e: caught Exception
    :param msg: Message to User with optional %s
    """
    ctx = click.get_current_context()
    if ctx.obj['verbosity'] >= 1:
        raise e
    else:
        if '%s' in msg:
            click.echo(msg % e)
        else:
            click.echo(msg)
        ctx.exit(1)


def to_pathlib(ctx, param, value):
    if value:
        return Path(value)
    else:
        return None


class PathlibPath(click.ParamType):
    """A parameter which accepts a file or directory name and performs
    checks on it.  First of all, instead of returning an open file
    handle it returns just the filename.  Secondly, it can perform various
    basic checks about what the file or directory should be.

    Very similar to :class:`click.Path`, but returns a :class:`pathlib.Path`.

    :param exists: if set to true, the file or directory needs to exist for
                   this value to be valid.  If this is not required and a
                   file does indeed not exist, then all further checks are
                   silently skipped.
    :param file_okay: controls if a file is a possible value.
    :param dir_okay: controls if a directory is a possible value.
    :param writable: if true, a writable check is performed.
    :param readable: if true, a readable check is performed.
    :param resolve_path: if this is true, then the path is fully resolved
                         before the value is passed onwards.  This means
                         that it's absolute and symlinks are resolved.
    """
    def __init__(self, exists=False, file_okay=True, dir_okay=True,
                 writable=False, readable=True, resolve_path=False):
        self.exists = exists
        self.file_okay = file_okay
        self.dir_okay = dir_okay
        self.writable = writable
        self.readable = readable
        self.resolve_path = resolve_path

        if self.file_okay and not self.dir_okay:
            self.name = 'file'
            self.path_type = 'File'
        if self.dir_okay and not self.file_okay:
            self.name = 'directory'
            self.path_type = 'Directory'
        else:
            self.name = 'path'
            self.path_type = 'Path'

    def convert(self, value, param, ctx):
        rv = Path(value)

        if self.resolve_path:
            rv = rv.resolve()

        if not self.exists:
            return rv

        if self.exists and not rv.exists():
            self.fail('%s "%s" does not exist.' % (
                self.path_type,
                filename_to_ui(value)
            ), param, ctx)

        if not self.file_okay and rv.is_file():
            self.fail('%s "%s" is a file.' % (
                self.path_type,
                filename_to_ui(value)
            ), param, ctx)
        if not self.dir_okay and rv.is_dir():
            self.fail('%s "%s" is a directory.' % (
                self.path_type,
                filename_to_ui(value)
            ), param, ctx)
        if self.writable and not os.access(value, os.W_OK):
            self.fail('%s "%s" is not writable.' % (
                self.path_type,
                filename_to_ui(value)
            ), param, ctx)
        if self.readable and not os.access(value, os.R_OK):
            self.fail('%s "%s" is not readable.' % (
                self.path_type,
                filename_to_ui(value)
            ), param, ctx)

        return rv


def parsed_search_expressions(f):
    """
    Add [expression] arguments and --crs option to a click application

    Passes a parsed dict of search expressions to the `expressions` argument
    of the command. The dict may include a `crs`.

    Also appends documentation on using search expressions to the command.

    WARNING: This wrapper expects an unlimited number of search expressions
    as click arguments, which means your command must take only click options
    or a specified number of arguments.
    """
    if not f.__doc__:
        f.__doc__ = ""
    f.__doc__ += """
    \b
    Search Expressions
    ------------------

    Select data using multiple [EXPRESSIONS] to limit by date, product type,
    spatial extent and other searchable fields.

    Specify either an Equals Expression with param=value, or a Range
    Expression with less<param<greater. Numbers or Dates are supported.

    Searchable fields include: x, y, time, product and more.

    NOTE: Range expressions using <,> symbols should be escaped with '', otherwise
    the shell will try to interpret them.

    \b
    eg. '1996-01-01 < time < 1996-12-31'
        '130<lon<140' '-30 > lat > -40'
        product=ls5_nbar_albers
    """

    def my_parse(ctx, param, value):
        parsed_expressions = parse_expressions(*list(value))
        # ctx.ensure_object(dict)
        # try:
        #     parsed_expressions['crs'] = ctx.obj['crs']
        # except KeyError:
        #     pass
        return parsed_expressions

    def store_crs(ctx, param, value):
        ctx.ensure_object(dict)
        # if value:
        #     ctx.obj['crs'] = value

    f = click.argument('expressions', callback=my_parse, nargs=-1)(f)
    # f = click.option('--crs', expose_value=False, help='Coordinate Reference used for x,y search expressions',
    #                  callback=store_crs)(f)
    return f
