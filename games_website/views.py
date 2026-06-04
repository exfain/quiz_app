from games_hub.views import join_session as hub_join_session


def home_page(request):
    return hub_join_session(request)
