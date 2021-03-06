from __future__ import print_function

import os
import re
import sys
import textwrap
import time

import click
import six
import yaml

from dagster import PipelineDefinition, check, execute_pipeline
from dagster.cli.load_handle import recon_pipeline_for_cli_args, recon_repo_for_cli_args
from dagster.cli.load_snapshot import get_pipeline_snapshot_from_cli_args
from dagster.core.definitions.executable import ExecutablePipeline
from dagster.core.definitions.partition import PartitionScheduleDefinition
from dagster.core.host_representation import InProcessRepositoryLocation
from dagster.core.instance import DagsterInstance
from dagster.core.snap import PipelineSnapshot, SolidInvocationSnap
from dagster.core.storage.pipeline_run import PipelineRun
from dagster.core.telemetry import log_repo_stats, telemetry_wrapper
from dagster.core.utils import make_new_backfill_id
from dagster.seven import IS_WINDOWS, JSONDecodeError, json
from dagster.utils import DEFAULT_REPOSITORY_YAML_FILENAME, load_yaml_from_glob_list, merge_dicts
from dagster.utils.error import serializable_error_info_from_exc_info
from dagster.utils.indenting_printer import IndentingPrinter

from .config_scaffolder import scaffold_pipeline_config


def create_pipeline_cli_group():
    group = click.Group(name="pipeline")
    group.add_command(pipeline_list_command)
    group.add_command(pipeline_print_command)
    group.add_command(pipeline_execute_command)
    group.add_command(pipeline_backfill_command)
    group.add_command(pipeline_scaffold_command)
    group.add_command(pipeline_launch_command)
    return group


REPO_TARGET_WARNING = (
    'Can only use ONE of --repository-yaml/-y, --python-file/-f, --module-name/-m.'
)
REPO_ARG_NAMES = ['repository_yaml', 'module_name', 'fn_name', 'python_file']


def apply_click_params(command, *click_params):
    for click_param in click_params:
        command = click_param(command)
    return command


def repository_target_argument(f):
    return apply_click_params(
        f,
        click.option(
            '--workspace', '-w', type=click.Path(exists=True), help=('Path to workspace file')
        ),
        click.option(
            '--repository-yaml',
            '-y',
            type=click.Path(exists=True),
            help=(
                'Path to config file. Defaults to ./{default_filename} if --python-file '
                'and --module-name are not specified'
            ).format(default_filename=DEFAULT_REPOSITORY_YAML_FILENAME),
        ),
        click.option(
            '--python-file',
            '-f',
            type=click.Path(exists=True),
            help='Specify python file where repository or pipeline function lives.',
        ),
        click.option(
            '--module-name', '-m', help='Specify module where repository or pipeline function lives'
        ),
        click.option('--fn-name', '-n', help='Function that returns either repository or pipeline'),
    )


def pipeline_target_command(f):
    # f = repository_config_argument(f)
    # nargs=-1 is used right now to make this argument optional
    # it can only handle 0 or 1 pipeline names
    # see .pipeline.recon_pipeline_for_cli_args
    return apply_click_params(
        f,
        click.option(
            '--repository-yaml',
            '-y',
            type=click.Path(exists=True),
            help=(
                'Path to config file. Defaults to ./{default_filename} if --python-file '
                'and --module-name are not specified'
            ).format(default_filename=DEFAULT_REPOSITORY_YAML_FILENAME),
        ),
        click.argument('pipeline_name', nargs=-1),
        click.option('--python-file', '-f', type=click.Path(exists=True)),
        click.option('--module-name', '-m'),
        click.option('--fn-name', '-n'),
    )


@click.command(
    name='list',
    help="List the pipelines in a repository. {warning}".format(warning=REPO_TARGET_WARNING),
)
@repository_target_argument
def pipeline_list_command(**kwargs):
    return execute_list_command(kwargs, click.echo)


def execute_list_command(cli_args, print_fn):
    repository = recon_repo_for_cli_args(cli_args).get_definition()

    title = 'Repository {name}'.format(name=repository.name)
    print_fn(title)
    print_fn('*' * len(title))
    first = True
    for pipeline in repository.get_all_pipelines():
        pipeline_title = 'Pipeline: {name}'.format(name=pipeline.name)

        if not first:
            print_fn('*' * len(pipeline_title))
        first = False

        print_fn(pipeline_title)
        if pipeline.description:
            print_fn('Description:')
            print_fn(format_description(pipeline.description, indent=' ' * 4))
        print_fn('Solids: (Execution Order)')
        for solid in pipeline.solids_in_topological_order:
            print_fn('    ' + solid.name)


def format_description(desc, indent):
    check.str_param(desc, 'desc')
    check.str_param(indent, 'indent')
    desc = re.sub(r'\s+', ' ', desc)
    dedented = textwrap.dedent(desc)
    wrapper = textwrap.TextWrapper(initial_indent='', subsequent_indent=indent)
    filled = wrapper.fill(dedented)
    return filled


def get_pipeline_instructions(command_name):
    return (
        'This commands targets a pipeline. The pipeline can be specified in a number of ways:'
        '\n\n1. dagster pipeline {command_name} <<pipeline_name>> (works if .{default_filename} exists)'
        '\n\n2. dagster pipeline {command_name} <<pipeline_name>> -y path/to/{default_filename}'
        '\n\n3. dagster pipeline {command_name} -f /path/to/file.py -n define_some_pipeline'
        '\n\n4. dagster pipeline {command_name} -m a_module.submodule  -n define_some_pipeline'
        '\n\n5. dagster pipeline {command_name} -f /path/to/file.py -n define_some_repo <<pipeline_name>>'
        '\n\n6. dagster pipeline {command_name} -m a_module.submodule -n define_some_repo <<pipeline_name>>'
    ).format(command_name=command_name, default_filename=DEFAULT_REPOSITORY_YAML_FILENAME)


def get_partitioned_pipeline_instructions(command_name):
    return (
        'This commands targets a partitioned pipeline. The pipeline and partition set must be '
        'defined in a repository, which can be specified in a number of ways:'
        '\n\n1. dagster pipeline {command_name} <<pipeline_name>> (works if .{default_filename} exists)'
        '\n\n2. dagster pipeline {command_name} <<pipeline_name>> -y path/to/{default_filename}'
        '\n\n3. dagster pipeline {command_name} -f /path/to/file.py -n define_some_repo <<pipeline_name>>'
        '\n\n4. dagster pipeline {command_name} -m a_module.submodule -n define_some_repo <<pipeline_name>>'
    ).format(command_name=command_name, default_filename=DEFAULT_REPOSITORY_YAML_FILENAME)


@click.command(
    name='print',
    help='Print a pipeline.\n\n{instructions}'.format(
        instructions=get_pipeline_instructions('print')
    ),
)
@click.option('--verbose', is_flag=True)
@click.option('--image', type=click.STRING, help="Built image name:tag that holds user code.")
@pipeline_target_command
def pipeline_print_command(verbose, **cli_args):
    return execute_print_command(verbose, cli_args, click.echo)


def execute_print_command(verbose, cli_args, print_fn):
    pipeline_snapshot = get_pipeline_snapshot_from_cli_args(cli_args)

    if verbose:
        print_pipeline(pipeline_snapshot, print_fn=print_fn)
    else:
        print_solids(pipeline_snapshot, print_fn=print_fn)


def print_solids(pipeline_snapshot, print_fn):
    check.inst_param(pipeline_snapshot, 'pipeline', PipelineSnapshot)
    check.callable_param(print_fn, 'print_fn')

    printer = IndentingPrinter(indent_level=2, printer=print_fn)
    printer.line('Pipeline: {name}'.format(name=pipeline_snapshot.name))

    printer.line('Solids:')
    for solid in pipeline_snapshot.dep_structure_snapshot.solid_invocation_snaps:
        with printer.with_indent():
            printer.line('Solid: {name}'.format(name=solid.solid_name))


def print_pipeline(pipeline_snapshot, print_fn):
    check.inst_param(pipeline_snapshot, 'pipeline', PipelineSnapshot)
    check.callable_param(print_fn, 'print_fn')
    printer = IndentingPrinter(indent_level=2, printer=print_fn)
    printer.line('Pipeline: {name}'.format(name=pipeline_snapshot.name))
    print_description(printer, pipeline_snapshot.description)

    printer.line('Solids:')
    for solid in pipeline_snapshot.dep_structure_snapshot.solid_invocation_snaps:
        with printer.with_indent():
            print_solid(printer, pipeline_snapshot, solid)


def print_description(printer, desc):
    with printer.with_indent():
        if desc:
            printer.line('Description:')
            with printer.with_indent():
                printer.line(format_description(desc, printer.current_indent_str))


def print_solid(printer, pipeline_snapshot, solid_invocation_snap):
    check.inst_param(pipeline_snapshot, 'pipeline_snapshot', PipelineSnapshot)
    check.inst_param(solid_invocation_snap, 'solid_invocation_snap', SolidInvocationSnap)
    printer.line('Solid: {name}'.format(name=solid_invocation_snap.solid_name))
    with printer.with_indent():
        printer.line('Inputs:')
        for input_dep_snap in solid_invocation_snap.input_dep_snaps:
            with printer.with_indent():
                printer.line('Input: {name}'.format(name=input_dep_snap.input_name))

        printer.line('Outputs:')
        for output_def_snap in pipeline_snapshot.get_solid_def_snap(
            solid_invocation_snap.solid_def_name
        ).output_def_snaps:
            printer.line(output_def_snap.name)


@click.command(
    name='execute',
    help='Execute a pipeline.\n\n{instructions}'.format(
        instructions=get_pipeline_instructions('execute')
    ),
)
@pipeline_target_command
@click.option(
    '-e',
    '--env',
    type=click.Path(exists=True),
    multiple=True,
    help=(
        'Specify one or more environment files. These can also be file patterns. '
        'If more than one environment file is captured then those files are merged. '
        'Files listed first take precedence. They will smash the values of subsequent '
        'files at the key-level granularity. If the file is a pattern then you must '
        'enclose it in double quotes'
        '\n\nExample: '
        'dagster pipeline execute pandas_hello_world -e "pandas_hello_world/*.yaml"'
        '\n\nYou can also specify multiple files:'
        '\n\nExample: '
        'dagster pipeline execute pandas_hello_world -e pandas_hello_world/solids.yaml '
        '-e pandas_hello_world/env.yaml'
    ),
)
@click.option(
    '-p',
    '--preset',
    type=click.STRING,
    help='Specify a preset to use for this pipeline. Presets are defined on pipelines under '
    'preset_defs.',
)
@click.option(
    '-d', '--mode', type=click.STRING, help='The name of the mode in which to execute the pipeline.'
)
@click.option('--tags', type=click.STRING, help='JSON string of tags to use for this pipeline run')
@click.option(
    '-s',
    '--solid-selection',
    type=click.STRING,
    help=(
        'Specify the solid subselection to execute. It can be multiple clauses separated by commas.'
        'Examples:'
        '\n- "some_solid" will execute "some_solid" itself'
        '\n- "*some_solid" will execute "some_solid" and all its ancestors (upstream dependencies)'
        '\n- "*some_solid+++" will execute "some_solid", all its ancestors, and its descendants'
        '   (downstream dependencies) within 3 levels down'
        '\n- "*some_solid,other_solid_a,other_solid_b+" will execute "some_solid" and all its'
        '   ancestors, "other_solid_a" itself, and "other_solid_b" and its direct child solids'
    ),
)
@telemetry_wrapper
def pipeline_execute_command(env, preset, mode, **kwargs):
    check.invariant(isinstance(env, tuple))

    if preset:
        if env:
            raise click.UsageError('Can not use --preset with --env.')
        return execute_execute_command_with_preset(preset, kwargs, mode)

    env = list(env)
    tags = get_tags_from_args(kwargs)

    execute_execute_command(env, kwargs, mode, tags)


def execute_execute_command(env, cli_args, mode=None, tags=None):
    pipeline = recon_pipeline_for_cli_args(cli_args)
    solid_selection = get_solid_selection_from_args(cli_args)
    return do_execute_command(pipeline, env, mode, tags, solid_selection)


def execute_execute_command_with_preset(preset_name, cli_args, _mode):
    pipeline = recon_pipeline_for_cli_args(cli_args)
    tags = get_tags_from_args(cli_args)
    solid_selection = get_solid_selection_from_args(cli_args)

    return execute_pipeline(
        pipeline,
        preset=preset_name,
        instance=DagsterInstance.get(),
        raise_on_error=False,
        tags=tags,
        solid_selection=solid_selection,
    )


def do_execute_command(pipeline, env_file_list, mode=None, tags=None, solid_selection=None):
    check.inst_param(pipeline, 'pipeline', ExecutablePipeline)
    env_file_list = check.opt_list_param(env_file_list, 'env_file_list', of_type=str)

    environment_dict = load_yaml_from_glob_list(env_file_list) if env_file_list else {}

    return execute_pipeline(
        pipeline,
        environment_dict=environment_dict,
        mode=mode,
        tags=tags,
        instance=DagsterInstance.get(),
        raise_on_error=False,
        solid_selection=solid_selection,
    )


@click.command(
    name='launch',
    help='Launch a pipeline using the run launcher configured on the Dagster instance.\n\n{instructions}'.format(
        instructions=get_pipeline_instructions('launch')
    ),
)
@pipeline_target_command
@click.option(
    '-e',
    '--env',
    type=click.Path(exists=True),
    multiple=True,
    help=(
        'Specify one or more environment files. These can also be file patterns. '
        'If more than one environment file is captured then those files are merged. '
        'Files listed first take precedence. They will smash the values of subsequent '
        'files at the key-level granularity. If the file is a pattern then you must '
        'enclose it in double quotes'
        '\n\nExample: '
        'dagster pipeline launch pandas_hello_world -e "pandas_hello_world/*.yaml"'
        '\n\nYou can also specify multiple files:'
        '\n\nExample: '
        'dagster pipeline launch pandas_hello_world -e pandas_hello_world/solids.yaml '
        '-e pandas_hello_world/env.yaml'
    ),
)
@click.option(
    '-p',
    '--preset-name',
    '--preset',
    type=click.STRING,
    help='Specify a preset to use for this pipeline. Presets are defined on pipelines under '
    'preset_defs.',
)
@click.option(
    '-d', '--mode', type=click.STRING, help='The name of the mode in which to execute the pipeline.'
)
@click.option('--tags', type=click.STRING, help='JSON string of tags to use for this pipeline run')
@click.option(
    '-s',
    '--solid-selection',
    type=click.STRING,
    help=(
        'Specify the solid subselection to launch. It can be multiple clauses separated by commas.'
        'Examples:'
        '\n- "some_solid" will launch "some_solid" itself'
        '\n- "*some_solid" will launch "some_solid" and all its ancestors (upstream dependencies)'
        '\n- "*some_solid+++" will launch "some_solid", all its ancestors, and its descendants'
        '   (downstream dependencies) within 3 levels down'
        '\n- "*some_solid,other_solid_a,other_solid_b+" will launch "some_solid" and all its'
        '   ancestors, "other_solid_a" itself, and "other_solid_b" and its direct child solids'
    ),
)
@telemetry_wrapper
def pipeline_launch_command(env, preset_name, mode, **kwargs):
    env = list(check.opt_tuple_param(env, 'env', default=(), of_type=str))
    pipeline = recon_pipeline_for_cli_args(kwargs)

    instance = DagsterInstance.get()
    log_repo_stats(instance=instance, pipeline=pipeline, source='pipeline_launch_command')

    if preset_name:
        if env:
            raise click.UsageError('Can not use --preset with --env.')

        if mode:
            raise click.UsageError('Can not use --preset with --mode.')

        preset = pipeline.get_preset(preset_name)
    else:
        preset = None

    run_tags = get_tags_from_args(kwargs)

    solid_selection = get_solid_selection_from_args(kwargs)

    if preset and preset.solid_selection is not None:
        check.invariant(
            solid_selection is None or solid_selection == preset.solid_selection,
            'The solid_selection set in preset \'{preset}\', {preset_subset}, does not agree with '
            'the `solid_selection` argument: {solid_selection}'.format(
                preset=preset,
                preset_subset=preset.solid_selection,
                solid_selection=solid_selection,
            ),
        )
        solid_selection = preset.solid_selection

    # generate pipeline subset from the given solid_selection
    if solid_selection:
        pipeline = pipeline.subset_for_execution(solid_selection)

    # FIXME need to check the env against environment_dict
    pipeline_run = instance.create_run_for_pipeline(
        pipeline_def=pipeline.get_definition(),
        solid_selection=solid_selection,
        solids_to_execute=pipeline.solids_to_execute,
        environment_dict=preset.environment_dict if preset else load_yaml_from_glob_list(env),
        mode=(preset.mode if preset else mode) or 'default',
        tags=run_tags,
    )

    recon_repo = pipeline.get_reconstructable_repository()

    repo_location = InProcessRepositoryLocation(recon_repo)
    external_pipeline = (
        repo_location.get_repository(recon_repo.get_definition().name).get_full_external_pipeline(
            pipeline.get_definition().name
        ),
    )

    return instance.launch_run(pipeline_run.run_id, external_pipeline)


@click.command(
    name='scaffold_config',
    help='Scaffold the config for a pipeline.\n\n{instructions}'.format(
        instructions=get_pipeline_instructions('scaffold_config')
    ),
)
@pipeline_target_command
@click.option('-p', '--print-only-required', default=False, is_flag=True)
def pipeline_scaffold_command(**kwargs):
    execute_scaffold_command(kwargs, click.echo)


def execute_scaffold_command(cli_args, print_fn):
    pipeline = recon_pipeline_for_cli_args(cli_args)
    skip_non_required = cli_args['print_only_required']
    do_scaffold_command(pipeline.get_definition(), print_fn, skip_non_required)


def do_scaffold_command(pipeline_def, printer, skip_non_required):
    check.inst_param(pipeline_def, 'pipeline_def', PipelineDefinition)
    check.callable_param(printer, 'printer')
    check.bool_param(skip_non_required, 'skip_non_required')

    config_dict = scaffold_pipeline_config(pipeline_def, skip_non_required=skip_non_required)
    yaml_string = yaml.dump(config_dict, default_flow_style=False)
    printer(yaml_string)


def gen_partitions_from_args(partition_set, kwargs):
    partition_selector_args = [
        bool(kwargs.get('all')),
        bool(kwargs.get('partitions')),
        (bool(kwargs.get('from')) or bool(kwargs.get('to'))),
    ]
    if sum(partition_selector_args) > 1:
        raise click.UsageError(
            'error, cannot use more than one of: `--all`, `--partitions`, `--from/--to`'
        )

    partitions = partition_set.get_partitions()

    if kwargs.get('all'):
        return partitions

    if kwargs.get('partitions'):
        selected_args = [s.strip() for s in kwargs.get('partitions').split(',') if s.strip()]
        selected_partitions = [
            partition for partition in partitions if partition.name in selected_args
        ]
        if len(selected_partitions) < len(selected_args):
            selected_names = [partition.name for partition in selected_partitions]
            unknown = [selected for selected in selected_args if selected not in selected_names]
            raise click.UsageError('Unknown partitions: {}'.format(unknown.join(', ')))
        return selected_partitions

    start = validate_partition_slice(partitions, 'from', kwargs.get('from'))
    end = validate_partition_slice(partitions, 'to', kwargs.get('to'))

    return partitions[start:end]


def get_tags_from_args(kwargs):
    if kwargs.get('tags') is None:
        return {}
    try:
        return json.loads(kwargs.get('tags'))
    except JSONDecodeError:
        raise click.UsageError(
            'Invalid JSON-string given for `--tags`: {}\n\n{}'.format(
                kwargs.get('tags'),
                serializable_error_info_from_exc_info(sys.exc_info()).to_string(),
            )
        )


def get_solid_selection_from_args(kwargs):
    solid_selection_str = kwargs.get('solid_selection')
    if not check.is_str(solid_selection_str):
        return None

    return [ele.strip() for ele in solid_selection_str.split(',')] if solid_selection_str else None


def print_partition_format(partitions, indent_level):
    if not IS_WINDOWS and sys.stdout.isatty():
        _, tty_width = os.popen('stty size', 'r').read().split()
        screen_width = min(250, int(tty_width))
    else:
        screen_width = 250
    max_str_len = max(len(x.name) for x in partitions)
    spacing = 10
    num_columns = min(10, int((screen_width - indent_level) / (max_str_len + spacing)))
    column_width = int((screen_width - indent_level) / num_columns)
    prefix = ' ' * max(0, indent_level - spacing)
    lines = []
    for chunk in list(split_chunk(partitions, num_columns)):
        lines.append(prefix + ''.join(partition.name.rjust(column_width) for partition in chunk))

    return '\n' + '\n'.join(lines)


def split_chunk(l, n):
    for i in range(0, len(l), n):
        yield l[i : i + n]


def validate_partition_slice(partitions, name, value):
    is_start = name == 'from'
    if value is None:
        return 0 if is_start else len(partitions)
    partition_names = [partition.name for partition in partitions]
    if value not in partition_names:
        raise click.UsageError('invalid value {} for {}'.format(value, name))
    index = partition_names.index(value)
    return index if is_start else index + 1


@click.command(
    name='backfill',
    help='Backfill a partitioned pipeline.\n\n{instructions}'.format(
        instructions=get_partitioned_pipeline_instructions('backfill')
    ),
)
@pipeline_target_command
@click.option(
    '-p',
    '--partitions',
    type=click.STRING,
    help='Comma-separated list of partition names that we want to backfill',
)
@click.option(
    '--partition-set',
    type=click.STRING,
    help='The name of the partition set over which we want to backfill.',
)
@click.option(
    '-a', '--all', type=click.STRING, help='Specify to select all partitions to backfill.',
)
@click.option(
    '--from',
    type=click.STRING,
    help=(
        'Specify a start partition for this backfill job'
        '\n\nExample: '
        'dagster pipeline backfill log_daily_stats --from 20191101'
    ),
)
@click.option(
    '--to',
    type=click.STRING,
    help=(
        'Specify an end partition for this backfill job'
        '\n\nExample: '
        'dagster pipeline backfill log_daily_stats --to 20191201'
    ),
)
@click.option('--tags', type=click.STRING, help='JSON string of tags to use for this pipeline run')
@click.option('--noprompt', is_flag=True)
def pipeline_backfill_command(**kwargs):
    execute_backfill_command(kwargs, click.echo)


def execute_backfill_command(cli_args, print_fn, instance=None):
    pipeline_name = cli_args.pop('pipeline_name')
    repo_args = {k: v for k, v in cli_args.items() if k in REPO_ARG_NAMES}
    if pipeline_name and not isinstance(pipeline_name, six.string_types):
        if len(pipeline_name) == 1:
            pipeline_name = pipeline_name[0]

    instance = instance or DagsterInstance.get()
    recon_repo = recon_repo_for_cli_args(repo_args)
    repo_def = recon_repo.get_definition()
    noprompt = cli_args.get('noprompt')

    # Resolve pipeline
    if not pipeline_name and noprompt:
        raise click.UsageError('No pipeline specified')
    if not pipeline_name:
        pipeline_name = click.prompt(
            'Select a pipeline to backfill: {}'.format(', '.join(repo_def.pipeline_names))
        )
    if not repo_def.has_pipeline(pipeline_name):
        raise click.UsageError('No pipeline found named `{}`'.format(pipeline_name))

    pipeline_def = repo_def.get_pipeline(pipeline_name)

    # Resolve partition set
    all_partition_sets = repo_def.partition_set_defs + [
        schedule_def.get_partition_set()
        for schedule_def in repo_def.schedule_defs
        if isinstance(schedule_def, PartitionScheduleDefinition)
    ]

    pipeline_partition_sets = [
        x for x in all_partition_sets if x.pipeline_name == pipeline_def.name
    ]
    if not pipeline_partition_sets:
        raise click.UsageError(
            'No partition sets found for pipeline `{}`'.format(pipeline_def.name)
        )
    partition_set_name = cli_args.get('partition_set')
    if not partition_set_name:
        if len(pipeline_partition_sets) == 1:
            partition_set_name = pipeline_partition_sets[0].name
        elif noprompt:
            raise click.UsageError('No partition set specified (see option `--partition-set`)')
        else:
            partition_set_name = click.prompt(
                'Select a partition set to use for backfill: {}'.format(
                    ', '.join(x.name for x in pipeline_partition_sets)
                )
            )
    partition_set = next((x for x in pipeline_partition_sets if x.name == partition_set_name), None)
    if not partition_set:
        raise click.UsageError('No partition set found named `{}`'.format(partition_set_name))

    # Resolve partitions to backfill
    partitions = gen_partitions_from_args(partition_set, cli_args)

    # Print backfill info
    print_fn('\n     Pipeline: {}'.format(pipeline_def.name))
    print_fn('Partition set: {}'.format(partition_set.name))
    print_fn('   Partitions: {}\n'.format(print_partition_format(partitions, indent_level=15)))

    # This whole CLI tool should move to more of a "host process" model - but this is how we start
    repo_location = InProcessRepositoryLocation(recon_repo)
    external_pipeline = (
        repo_location.get_repository(repo_def.name).get_full_external_pipeline(pipeline_name),
    )

    # Confirm and launch
    if noprompt or click.confirm(
        'Do you want to proceed with the backfill ({} partitions)?'.format(len(partitions))
    ):

        print_fn('Launching runs... ')
        backfill_id = make_new_backfill_id()

        run_tags = merge_dicts(
            PipelineRun.tags_for_backfill_id(backfill_id), get_tags_from_args(cli_args),
        )

        for partition in partitions:
            run = instance.create_run_for_pipeline(
                pipeline_def=pipeline_def,
                mode=partition_set.mode,
                solids_to_execute=frozenset(partition_set.solid_selection)
                if partition_set and partition_set.solid_selection
                else None,
                environment_dict=partition_set.environment_dict_for_partition(partition),
                tags=merge_dicts(partition_set.tags_for_partition(partition), run_tags),
            )

            instance.launch_run(run.run_id, external_pipeline)
            # Remove once we can handle synchronous execution... currently limited by sqlite
            time.sleep(0.1)

        print_fn('Launched backfill job `{}`'.format(backfill_id))
    else:
        print_fn(' Aborted!')


pipeline_cli = create_pipeline_cli_group()
