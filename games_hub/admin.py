from django.contrib import admin
from .models import HubGameParticipantSnapshot, HubSession, HubParticipant, HubGameStep


@admin.register(HubSession)
class HubSessionAdmin(admin.ModelAdmin):
    list_display = (
        'code',
        'name',
        'is_active',
        'check_in_status',
        'locked_participant_count',
        'created_at',
        'started_at',
        'ended_at',
        'current_step_index',
    )
    search_fields = ('code', 'name')


@admin.register(HubParticipant)
class HubParticipantAdmin(admin.ModelAdmin):
    list_display = (
        'nickname',
        'session',
        'is_active',
        'checked_in_at',
        'scoring_eligible',
        'check_in_excluded_by_host',
        'left_permanently_at',
        'joined_at',
        'last_seen',
    )
    search_fields = ('nickname', 'session__code')


@admin.register(HubGameStep)
class HubGameStepAdmin(admin.ModelAdmin):
    list_display = ('session', 'order', 'game_key', 'room_code', 'title')
    list_filter = ('game_key',)
    ordering = ('session', 'order')


@admin.register(HubGameParticipantSnapshot)
class HubGameParticipantSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        'session',
        'game_step',
        'participant',
        'included_in_scoring',
        'active_player',
        'auto_zero',
        'reason',
        'created_at',
    )
    list_filter = ('included_in_scoring', 'active_player', 'auto_zero', 'reason')
    search_fields = ('session__code', 'participant__nickname', 'game_step__title')
