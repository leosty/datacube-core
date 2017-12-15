from __future__ import absolute_import

import time
import logging
import click
import cachetools
import itertools

from datacube.drivers.manager import DriverManager

try:
    import cPickle as pickle
except ImportError:
    import pickle
from copy import deepcopy
from pathlib import Path
from pandas import to_datetime
from datetime import datetime

import datacube
from datacube.api.core import Datacube
from datacube.model import DatasetType, Range, GeoPolygon
from datacube.model.utils import make_dataset, xr_apply, datasets_to_doc
from datacube.ui import click as ui
from datacube.utils import read_documents, changes
from datacube.ui.task_app import check_existing_files, load_tasks as load_tasks_, save_tasks as save_tasks_

from datacube.ui.click import cli

_LOG = logging.getLogger('agdc-ingest')

FUSER_KEY = 'fuse_data'


def remove_duplicates(cells_in, cells_out):
    """Remove tiles in `cells_in` which belong to `cells_out`.

    Tiles are compared based on their signature: `(extent,
    timestamp)`.
    """
    if not cells_out:
        return

    # Compute signatures of cells_out
    sigs_out = {(extent, timestamp) \
                for extent, cell in cells_out.items() \
                for timestamp in cell.sources.coords['time'].values}
    # Index cells_in accordingly
    for extent, cell in cells_in.items():
        tiles = cell.sources
        to_add = [timestamp \
                  for timestamp in tiles.coords['time'].values \
                  if not (extent, timestamp) in sigs_out]
        cell.sources = tiles.loc[to_add]


def find_diff(input_type, output_type, driver_manager, time_size, **query):
    from datacube.api.grid_workflow import GridWorkflow
    workflow = GridWorkflow(None, output_type.grid_spec, driver_manager=driver_manager)

    cells_in = workflow.list_cells(product=input_type.name, **query)
    cells_out = workflow.list_cells(product=output_type.name, **query)

    remove_duplicates(cells_in, cells_out)
    tasks = [{'tile': cell, 'tile_index': extent} for extent, cell in cells_in.items()]
    new_tasks = []
    for task in tasks:
        tiles = task['tile'].split('time', time_size)
        for t in tiles:
            new_tasks.append({'tile': t[1], 'tile_index': task['tile_index']})

    return new_tasks


def morph_dataset_type(source_type, config, driver_manager):
    output_metadata_type = source_type.metadata_type
    if 'metadata_type' in config:
        output_metadata_type = driver_manager.index.metadata_types.get_by_name(config['metadata_type'])

    output_type = DatasetType(output_metadata_type, deepcopy(source_type.definition))
    output_type.definition['name'] = config['output_type']
    output_type.definition['managed'] = True
    output_type.definition['description'] = config['description']
    output_type.definition['storage'] = config['storage']
    output_type.definition['storage'] = {k: v for (k, v) in config['storage'].items()
                                         if k in ('crs', 'driver', 'tile_size', 'resolution', 'origin')}
    output_type.metadata_doc['format'] = {'name': driver_manager.driver.format}

    if 'metadata_type' in config:
        output_type.definition['metadata_type'] = config['metadata_type']

    def merge_measurement(measurement, spec):
        measurement.update({k: spec.get(k, measurement[k]) for k in ('name', 'nodata', 'dtype')})
        return measurement

    output_type.definition['measurements'] = [merge_measurement(output_type.measurements[spec['src_varname']], spec)
                                              for spec in config['measurements']]
    return output_type


def get_variable_params(config):
    chunking = config['storage']['chunking']
    chunking = [chunking[dim] for dim in config['storage']['dimension_order']]

    variable_params = {}
    for mapping in config['measurements']:
        varname = mapping['name']
        variable_params[varname] = {k: v for k, v in mapping.items() if k in {'zlib',
                                                                              'complevel',
                                                                              'shuffle',
                                                                              'fletcher32',
                                                                              'contiguous',
                                                                              'attrs'}}
        variable_params[varname]['chunksizes'] = chunking
        if 'container' in config:
            variable_params[varname]['container'] = config['container']

    return variable_params


def get_app_metadata(config, config_file):
    doc = {
        'lineage': {
            'algorithm': {
                'name': 'datacube-ingest',
                'version': config.get('version', 'unknown'),
                'repo_url': 'https://github.com/GeoscienceAustralia/datacube-ingester.git',
                'parameters': {'configuration_file': config_file}
            },
        }
    }
    return doc


def get_filename(config, tile_index, sources, **kwargs):
    file_path_template = str(Path(config['location'], config['file_path_template']))
    time_format = '%Y%m%d%H%M%S%f'
    return Path(file_path_template.format(
        tile_index=tile_index,
        start_time=to_datetime(sources.time.values[0]).strftime(time_format),
        end_time=to_datetime(sources.time.values[-1]).strftime(time_format),
        version=config['taskfile_version'],
        **kwargs))


def get_measurements(source_type, config):
    def merge_measurement(measurement, spec):
        measurement.update({k: spec.get(k) or measurement[k] for k in ('nodata', 'dtype', 'resampling_method')})
        return measurement

    return [merge_measurement(source_type.measurements[spec['src_varname']].copy(), spec)
            for spec in config['measurements']]


def get_namemap(config):
    return {spec['src_varname']: spec['name'] for spec in config['measurements']}


def ensure_output_type(driver_manager, config, allow_product_changes=False):
    # type: (DriverManager, dict, bool) -> (DatasetType, DatasetType)
    """
    Create the output product for the given ingest config if it doesn't already exist.

    It will throw a ValueError if the config already exists but differs from the existing.
    Set allow_product_changes=True to allow changes.
    """

    index = driver_manager.index
    source_type = index.products.get_by_name(config['source_type'])
    if not source_type:
        click.echo("Source DatasetType %s does not exist" % config['source_type'])
        click.get_current_context().exit(1)

    output_type = morph_dataset_type(source_type, config, driver_manager)
    _LOG.info('Created DatasetType %s', output_type.name)

    existing = index.products.get_by_name(output_type.name)
    if existing:
        can_update, safe_changes, unsafe_changes = index.products.can_update(output_type)
        if safe_changes or unsafe_changes:
            if not allow_product_changes:
                raise ValueError("Ingest config differs from the existing output product, "
                                 "but allow_product_changes=False")
            output_type = index.products.update(output_type)
    else:
        output_type = index.products.add(output_type)

    return source_type, output_type


@cachetools.cached(cache={}, key=lambda index, id_: id_)
def get_full_lineage(index, id_):
    return index.datasets.get(id_, include_sources=True)


def load_config_from_file(index, config):
    config_name = Path(config).name
    _, config = next(read_documents(Path(config)))
    config['filename'] = config_name

    return config


def create_task_list(driver_manager, output_type, year, source_type, config):
    config['taskfile_version'] = int(time.time())

    query = {}
    if year:
        query['time'] = Range(datetime(year=year[0], month=1, day=1), datetime(year=year[1] + 1, month=1, day=1))
    if 'ingestion_bounds' in config:
        bounds = config['ingestion_bounds']
        query['x'] = Range(bounds['left'], bounds['right'])
        query['y'] = Range(bounds['bottom'], bounds['top'])

    time_size = 1
    if 'time' in config['storage']['tile_size']:
        time_size = config['storage']['tile_size']['time']

    tasks = find_diff(source_type, output_type, driver_manager, time_size, **query)
    _LOG.info('%s tasks discovered', len(tasks))

    def check_valid(tile, tile_index):
        if FUSER_KEY in config:
            return True

        require_fusing = [source for source in tile.sources.values if len(source) > 1]
        if require_fusing:
            _LOG.warning('Skipping %s - no "%s" specified in config: %s', tile_index, FUSER_KEY, require_fusing)

        return not require_fusing

    def update_sources(sources):
        return tuple(get_full_lineage(driver_manager.index, dataset.id) for dataset in sources)

    def update_task(task):
        tile = task['tile']
        for i in range(tile.sources.size):
            tile.sources.values[i] = update_sources(tile.sources.values[i])
        return task

    tasks = (update_task(task) for task in tasks if check_valid(**task))
    return tasks


def ingest_work(driver_manager, config, source_type, output_type, tile, tile_index):
    _LOG.info('Starting task %s', tile_index)

    namemap = get_namemap(config)
    measurements = get_measurements(source_type, config)
    variable_params = get_variable_params(config)
    global_attributes = config['global_attributes']

    with datacube.set_options(reproject_threads=1):
        fuse_func = {'copy': None}[config.get(FUSER_KEY, 'copy')]
        data = Datacube.load_data(tile.sources, tile.geobox, measurements,
                                  fuse_func=fuse_func, driver_manager=driver_manager)
    nudata = data.rename(namemap)
    file_path = get_filename(config, tile_index, tile.sources)

    def _make_dataset(labels, sources):
        return make_dataset(product=output_type,
                            sources=sources,
                            extent=tile.geobox.extent,
                            center_time=labels['time'],
                            uri=file_path.absolute().as_uri(),
                            app_info=get_app_metadata(config, config['filename']),
                            valid_data=GeoPolygon.from_sources_extents(sources, tile.geobox))

    datasets = xr_apply(tile.sources, _make_dataset, dtype='O')  # Store in Dataarray to associate Time -> Dataset
    nudata['dataset'] = datasets_to_doc(datasets)

    # Until ingest becomes a class and DriverManager an instance
    # variable, we call the constructor each time. DriverManager being
    # a singleton, there is little overhead, though.
    datasets.attrs['storage_output'] = driver_manager.write_dataset_to_storage(nudata,
                                                                               file_path,
                                                                               global_attributes,
                                                                               variable_params)
    _LOG.info('Finished task %s', tile_index)

    # When using multiproc executor, Driver Manager is a clone.
    if driver_manager.is_clone:
        driver_manager.close()

    return datasets


def _index_datasets(driver_manager, results):
    n = 0
    for datasets in results:
        n += driver_manager.index_datasets(datasets, sources_policy='verify')
    return n


def process_tasks(driver_manager, config, source_type, output_type, tasks, queue_size, executor):
    def submit_task(task):
        _LOG.info('Submitting task: %s', task['tile_index'])
        return executor.submit(ingest_work,
                               driver_manager=driver_manager,
                               config=config,
                               source_type=source_type,
                               output_type=output_type,
                               **task)

    pending = []
    n_successful = n_failed = 0

    tasks = iter(tasks)
    while True:
        pending += [submit_task(task) for task in itertools.islice(tasks, max(0, queue_size - len(pending)))]
        if not pending:
            break

        completed, failed, pending = executor.get_ready(pending)
        _LOG.info('completed %s, failed %s, pending %s', len(completed), len(failed), len(pending))

        for future in failed:
            try:
                executor.result(future)
            except Exception:  # pylint: disable=broad-except
                _LOG.exception('Task failed')
                n_failed += 1

        if not completed:
            time.sleep(1)
            continue

        try:
            # TODO: ideally we wouldn't block here indefinitely
            # maybe limit gather to 50-100 results and put the rest into a index backlog
            # this will also keep the queue full
            n_successful += _index_datasets(driver_manager, executor.results(completed))
        except Exception:  # pylint: disable=broad-except
            _LOG.exception('Gather failed')
            pending += completed

    return n_successful, n_failed


def _validate_year(ctx, param, value):
    try:
        if value is None:
            return None
        years = list(map(int, value.split('-', 2)))
        if len(years) == 1:
            return years[0], years[0]
        return tuple(years)
    except ValueError:
        raise click.BadParameter('year must be specified as a single year (eg 1996) '
                                 'or as an inclusive range (eg 1996-2001)')


@cli.command('ingest', help="Ingest datasets")
@click.option('--config-file', '-c',
              type=ui.PathlibPath(exists=True, readable=True, writable=False, dir_okay=False),
              help='Ingest configuration file')
@click.option('--year', callback=_validate_year, help='Limit the process to a particular year')
@click.option('--queue-size', type=click.IntRange(1, 100000), default=3200, help='Task queue size')
@click.option('--save-tasks', help='Save tasks to the specified file',
              type=ui.PathlibPath(exists=False))
@click.option('--load-tasks', help='Load tasks from the specified file',
              type=ui.PathlibPath(exists=True, readable=True, writable=False, dir_okay=False))
@click.option('--dry-run', '-d', is_flag=True, default=False, help='Check if everything is ok')
@click.option('--allow-product-changes', is_flag=True, default=False,
              help='Allow the output product definition to be updated if it differs.')
@ui.executor_cli_options
@ui.pass_driver_manager(app_name='agdc-ingest')
def ingest_cmd(driver_manager,
               config_file,
               year,
               queue_size,
               save_tasks,
               load_tasks,
               dry_run,
               executor,
               allow_product_changes):
    index = driver_manager.index
    if config_file:
        config = load_config_from_file(index, config_file)
        source_type, output_type = ensure_output_type(driver_manager, config,
                                                      allow_product_changes=allow_product_changes)

        tasks = create_task_list(driver_manager, output_type, year, source_type, config)
    elif load_tasks:
        config, tasks = load_tasks_(load_tasks)
        source_type, output_type = ensure_output_type(driver_manager, config,
                                                      allow_product_changes=allow_product_changes)
    else:
        click.echo('Must specify exactly one of --config-file, --load-tasks')
        return 1

    if dry_run:
        check_existing_files(get_filename(config, task['tile_index'], task['tile'].sources) for task in tasks)
        return 0

    if save_tasks:
        save_tasks_(config, tasks, save_tasks)
        return 0

    successful, failed = process_tasks(driver_manager, config, source_type, output_type, tasks, queue_size, executor)
    click.echo('%d successful, %d failed' % (successful, failed))

    return 0
