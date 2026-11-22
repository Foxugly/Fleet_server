from rest_framework import serializers

STATES = ["green", "orange", "red", "stale", "error", "none"]


class ReasonSerializer(serializers.Serializer):
    code = serializers.CharField()
    params = serializers.DictField()


class CellSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=STATES)
    count = serializers.IntegerField()
    collected_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField(allow_blank=True)
    reason = ReasonSerializer(allow_null=True)


class SiteCellsSerializer(serializers.Serializer):
    health = CellSerializer()
    github = CellSerializer()
    sentry = CellSerializer()
    deps = CellSerializer()


class OverviewSiteSerializer(serializers.Serializer):
    slug = serializers.CharField()
    deployed = serializers.BooleanField()
    cells = SiteCellsSerializer()


class OverviewBoxSerializer(serializers.Serializer):
    box = CellSerializer()


class OverviewSerializer(serializers.Serializer):
    generated_at = serializers.DateTimeField()
    box = OverviewBoxSerializer()
    sites = OverviewSiteSerializer(many=True)


class SubjectSerializer(serializers.Serializer):
    data = serializers.JSONField(allow_null=True)
    collected_at = serializers.DateTimeField(allow_null=True)
    ok = serializers.BooleanField(allow_null=True)
    error = serializers.CharField(allow_blank=True)


class SourceDetailSerializer(serializers.Serializer):
    cell = CellSerializer()
    items = serializers.DictField(child=SubjectSerializer())


class SiteSourcesSerializer(serializers.Serializer):
    health = SourceDetailSerializer()
    github = SourceDetailSerializer()
    sentry = SourceDetailSerializer()
    deps = SourceDetailSerializer()


class SiteDetailSerializer(serializers.Serializer):
    slug = serializers.CharField()
    repos = serializers.ListField(child=serializers.CharField())
    vhosts = serializers.ListField(child=serializers.CharField())
    deployed = serializers.BooleanField()
    actions_allowed = serializers.BooleanField()
    sources = SiteSourcesSerializer()


class BoxSerializer(serializers.Serializer):
    cell = CellSerializer()
    data = serializers.JSONField(allow_null=True)
    collected_at = serializers.DateTimeField(allow_null=True)
    ok = serializers.BooleanField(allow_null=True)
    error = serializers.CharField(allow_blank=True)


class QueuedSerializer(serializers.Serializer):
    queued = serializers.CharField()
    already_queued = serializers.BooleanField(required=False)
