from django.contrib import admin
from .models import ClueRushGame, Clue, ClueRushParticipant


# class ClueInline(admin.TabularInline):
#     model = Clue
#     extra = 1
#     fields = ("order", "text", "duration_seconds")


# @admin.register(ClueRushGame)
# class ClueRushGameAdmin(admin.ModelAdmin):
#     list_display = ("title", "room_code", "status", "created_at", "started_at", "ended_at")
#     list_filter = ("status",)
#     search_fields = ("title", "room_code")
#     inlines = [ClueInline]
#     fields = ("title", "room_code", "creator", "status", "answer_text", "max_participants")
#     readonly_fields = ("room_code",)


# @admin.register(ClueRushParticipant)
# class ClueRushParticipantAdmin(admin.ModelAdmin):
#     list_display = ("name", "quiz", "total_score", "has_guessed", "guess_correct")
#     list_filter = ("has_guessed", "guess_correct")
#     search_fields = ("name", "game__room_code")

