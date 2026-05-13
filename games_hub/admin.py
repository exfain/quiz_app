from django.contrib import admin
from .models import HubSession, HubParticipant, HubGameStep


@admin.register(HubSession)
class HubSessionAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'is_active', 'created_at', 'started_at', 'ended_at', 'current_step_index')
    search_fields = ('code', 'name')


@admin.register(HubParticipant)
class HubParticipantAdmin(admin.ModelAdmin):
    list_display = ('nickname', 'session', 'is_active', 'joined_at', 'last_seen')
    search_fields = ('nickname', 'session__code')


@admin.register(HubGameStep)
class HubGameStepAdmin(admin.ModelAdmin):
    list_display = ('session', 'order', 'game_key', 'room_code', 'title')
    list_filter = ('game_key',)
    ordering = ('session', 'order')
