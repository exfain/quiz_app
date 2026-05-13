"""
URL configuration for games_website project.
"""
from . import views
from django.contrib import admin
from django.urls import path, include
from django.shortcuts import redirect
from django.conf import settings
from django.conf.urls.static import static



urlpatterns = [
    path('admin/', admin.site.urls),
    
    # Authentication URLs (Django's built-in auth views)
    path('accounts/', include('django.contrib.auth.urls')),
    
    # Home redirect
    path('', views.home_page, name='home'),
    
    # Admin dashboard (requires admin access)
    path('admin-dashboard/', include('admin_dashboard.urls')),

    # Quiz game (public access)
    path('quiz/', include('QuizGame.urls')),

    # Assign game

    path("assign/", include("Assign.urls")),
    
    # Where is this game (public access)

    path('estimation/', include("Estimation.urls")),    
    
    path('where/', include('where_is_this.urls')),

    # Who is Lying game (public access)
    path('who/', include('who_is_lying.urls')),  


    path('who-is-that/', include('who_is_that.urls')),  

    path('blackjack/', include("black_jack_quiz.urls")),

    # Central games hub
    path('hub/', include('games_hub.urls')),

    # Clue Rush game
    path('clue-rush/', include('clue_rush.urls')),

    # Sorting Ladder game
    path('sorting-ladder/', include('sorting_ladder.urls')),

]

# Serve media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)