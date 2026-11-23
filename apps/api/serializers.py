from rest_framework import serializers

STATES = ["green", "orange", "red", "stale", "error", "none"]
#: `UpdateBlocker.kind`; named `BlockerKindEnum` in the schema (settings).
BLOCKER_KINDS = ["package", "hold"]


class ReasonSerializer(serializers.Serializer):
    code = serializers.CharField()
    params = serializers.DictField()


class CellSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=STATES)
    count = serializers.IntegerField()
    collected_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField(allow_blank=True)
    reason = ReasonSerializer(allow_null=True)
    notes = ReasonSerializer(many=True)


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
    archived = serializers.BooleanField()
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


class UpdateMemberSerializer(serializers.Serializer):
    site = serializers.CharField()
    repo = serializers.CharField()
    lock = serializers.CharField(allow_null=True)
    current = serializers.CharField(allow_null=True)


class UpdateSecuritySerializer(serializers.Serializer):
    alerts = serializers.IntegerField()
    max_severity = serializers.CharField(allow_null=True)


class UpdatePrSerializer(serializers.Serializer):
    repo = serializers.CharField()
    number = serializers.IntegerField()
    url = serializers.CharField()
    checks = serializers.CharField()


class UpdateIssueSerializer(serializers.Serializer):
    repo = serializers.CharField()
    number = serializers.IntegerField()
    url = serializers.CharField()


class UpdateBlockerSerializer(serializers.Serializer):
    #: `package`: an installed package holds the update back; `hold`: the
    #: project's requirements.in caps it on purpose (`package` is then the file
    #: name, `version` is empty and `note` its comment).
    kind = serializers.ChoiceField(choices=BLOCKER_KINDS)
    package = serializers.CharField()
    version = serializers.CharField(allow_blank=True)
    newer = serializers.CharField(allow_null=True)
    #: The blocker's declared range (automatic blocks), null for issue-based ones.
    range = serializers.CharField(allow_null=True)
    #: The hold's comment in requirements.in (untrusted plain text), else "".
    note = serializers.CharField(allow_blank=True)


class UpdateBlockedSerializer(serializers.Serializer):
    issues = UpdateIssueSerializer(many=True)
    by = UpdateBlockerSerializer(many=True)


class UpdateGroupSerializer(serializers.Serializer):
    ecosystem = serializers.CharField(allow_null=True)
    package = serializers.CharField()
    latest = serializers.CharField(allow_null=True)
    kind = serializers.CharField(allow_null=True)
    members = UpdateMemberSerializer(many=True)
    security = UpdateSecuritySerializer(allow_null=True)
    branch = serializers.CharField()
    prs = UpdatePrSerializer(many=True)
    progress = serializers.ChoiceField(
        choices=[
            "none",
            "in_progress",
            "possible",
            "blocked",
            "waiting",
            "retry",
        ]
    )
    blocked = UpdateBlockedSerializer(allow_null=True)


class UpdatesSerializer(serializers.Serializer):
    generated_at = serializers.DateTimeField()
    groups = UpdateGroupSerializer(many=True)


class AppOutSerializer(serializers.Serializer):
    slug = serializers.CharField()
    repos = serializers.ListField(child=serializers.CharField())
    vhosts = serializers.ListField(child=serializers.CharField())
    health_path = serializers.CharField()
    sentry_projects = serializers.ListField(child=serializers.CharField())
    python_locks = serializers.ListField(child=serializers.CharField())
    npm_locks = serializers.ListField(child=serializers.CharField())
    deployed = serializers.BooleanField()
    actions_allowed = serializers.BooleanField()
    archived = serializers.BooleanField()
    archived_at = serializers.DateTimeField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class AppUpdateSerializer(serializers.Serializer):
    """Documentation of the PUT body; the rules live in `clean_app`."""

    repos = serializers.ListField(child=serializers.CharField(), required=False)
    vhosts = serializers.ListField(child=serializers.CharField(), required=False)
    health_path = serializers.CharField(required=False, max_length=200)
    sentry_projects = serializers.ListField(
        child=serializers.CharField(), required=False
    )
    python_locks = serializers.ListField(child=serializers.CharField(), required=False)
    npm_locks = serializers.ListField(child=serializers.CharField(), required=False)
    deployed = serializers.BooleanField()
    actions_allowed = serializers.BooleanField()
    totp_code = serializers.CharField()


class AppCreateSerializer(AppUpdateSerializer):
    """Documentation of the POST body: the same plus the (required) slug."""

    slug = serializers.CharField(max_length=64)


class TotpOnlySerializer(serializers.Serializer):
    totp_code = serializers.CharField()


class AppChangeSerializer(serializers.Serializer):
    at = serializers.DateTimeField()
    user = serializers.CharField(allow_null=True)
    action = serializers.CharField()
    before = serializers.JSONField(allow_null=True)
    after = serializers.JSONField(allow_null=True)
