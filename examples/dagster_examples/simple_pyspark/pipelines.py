'''Pipeline definitions for the simple_pyspark example.'''
from dagster_aws.emr import emr_pyspark_step_launcher
from dagster_aws.s3 import s3_plus_default_storage_defs, s3_resource
from dagster_pyspark import pyspark_resource

from dagster import ModeDefinition, PresetDefinition, pipeline
from dagster.core.definitions.no_step_launcher import no_step_launcher

from .solids import (
    make_daily_temperature_high_diffs,
    make_daily_temperature_highs,
    make_weather_samples,
)

local_mode = ModeDefinition(
    name='local',
    resource_defs={'pyspark_step_launcher': no_step_launcher, 'pyspark': pyspark_resource},
)


prod_mode = ModeDefinition(
    name='prod',
    resource_defs={
        'pyspark_step_launcher': emr_pyspark_step_launcher,
        'pyspark': pyspark_resource,
        's3': s3_resource,
    },
    system_storage_defs=s3_plus_default_storage_defs,
)


@pipeline(
    mode_defs=[local_mode, prod_mode],
    preset_defs=[
        PresetDefinition.from_pkg_resources(
            name='local',
            mode='local',
            pkg_resource_defs=[
                ('dagster_examples.simple_pyspark.environments', 'local.yaml'),
                ('dagster_examples.simple_pyspark.environments', 'filesystem_storage.yaml'),
            ],
        ),
        PresetDefinition.from_pkg_resources(
            name='prod',
            mode='prod',
            pkg_resource_defs=[
                ('dagster_examples.simple_pyspark.environments', 'prod.yaml'),
                ('dagster_examples.simple_pyspark.environments', 's3_storage.yaml'),
            ],
        ),
    ],
)
def simple_pyspark_sfo_weather_pipeline():
    '''Computes some basic statistics over weather data from SFO airport'''
    make_daily_temperature_high_diffs(make_daily_temperature_highs(make_weather_samples()),)


def define_simple_pyspark_sfo_weather_pipeline():
    return simple_pyspark_sfo_weather_pipeline
