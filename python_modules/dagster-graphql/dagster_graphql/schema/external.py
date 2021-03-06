from __future__ import absolute_import

from dagster_graphql import dauphin
from dagster_graphql.implementation.context import RepositoryLocation
from dagster_graphql.implementation.fetch_solids import get_solid, get_solids

from dagster import check
from dagster.core.host_representation import ExternalRepository


class DauphinRepositoryLocation(dauphin.ObjectType):
    class Meta(object):
        name = 'RepositoryLocation'

    name = dauphin.NonNull(dauphin.String)
    repositories = dauphin.non_null_list('Repository')

    def __init__(self, location):
        self._location = check.inst_param(location, 'location', RepositoryLocation)
        super(DauphinRepositoryLocation, self).__init__(name=location.name)

    def resolve_repositories(self, graphene_info):
        return [
            graphene_info.schema.type_named('Repository')(repository)
            for repository in self._location.get_repositories().values()
        ]


class DauphinRepository(dauphin.ObjectType):
    class Meta(object):
        name = 'Repository'

    def __init__(self, repository):
        self._repository = check.inst_param(repository, 'repository', ExternalRepository)
        super(DauphinRepository, self).__init__(name=repository.name)

    name = dauphin.NonNull(dauphin.String)
    pipelines = dauphin.non_null_list('Pipeline')
    usedSolids = dauphin.Field(dauphin.non_null_list('UsedSolid'))
    usedSolid = dauphin.Field('UsedSolid', name=dauphin.NonNull(dauphin.String))

    def resolve_pipelines(self, graphene_info):
        return sorted(
            [
                graphene_info.schema.type_named('Pipeline')(pipeline)
                for pipeline in self._repository.get_all_external_pipelines()
            ],
            key=lambda pipeline: pipeline.name,
        )

    def resolve_usedSolid(self, _graphene_info, name):
        return get_solid(self._repository, name)

    def resolve_usedSolids(self, _graphene_info):
        return get_solids(self._repository)


class DauphinRepositoryLocationConnection(dauphin.ObjectType):
    class Meta(object):
        name = 'RepositoryLocationConnection'

    nodes = dauphin.non_null_list('RepositoryLocation')
