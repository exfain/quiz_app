from django.contrib import admin

from .models import (
    WerWeissMehrAnswerOption,
    WerWeissMehrGame,
    WerWeissMehrParticipant,
    WerWeissMehrParticipantState,
    WerWeissMehrPendingInput,
    WerWeissMehrQuestion,
    WerWeissMehrRound,
    WerWeissMehrRoundResponse,
    WerWeissMehrSession,
)


class WerWeissMehrAnswerInline(admin.TabularInline):
    model = WerWeissMehrAnswerOption
    extra = 1


@admin.register(WerWeissMehrQuestion)
class WerWeissMehrQuestionAdmin(admin.ModelAdmin):
    list_display = ('question_text', 'round_time_limit', 'created_by', 'is_active', 'created_at')
    search_fields = ('question_text', 'answers__canonical_text')
    list_filter = ('is_active', 'created_at')
    inlines = [WerWeissMehrAnswerInline]


@admin.register(WerWeissMehrGame)
class WerWeissMehrGameAdmin(admin.ModelAdmin):
    list_display = ('title', 'room_code', 'status', 'creator', 'created_at', 'started_at', 'ended_at')
    list_filter = ('status', 'created_at')
    search_fields = ('title', 'room_code')
    filter_horizontal = ('selected_questions',)


admin.site.register(WerWeissMehrParticipant)
admin.site.register(WerWeissMehrSession)
admin.site.register(WerWeissMehrRound)
admin.site.register(WerWeissMehrRoundResponse)
admin.site.register(WerWeissMehrParticipantState)
admin.site.register(WerWeissMehrPendingInput)
