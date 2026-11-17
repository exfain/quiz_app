"""Upgrade a real isolated SQLite backup, not the developer's database."""
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

from django.db import connection
from django.db.backends.sqlite3._functions import register
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from .migration_test_utils import targets_before


class OlderDatabaseCopyUpgradeTests(TransactionTestCase):
    def test_upgrade_hub_0024_copy_preserves_players_scores_and_adds_current_tables(self):
        executor=MigrationExecutor(connection)
        latest=executor.loader.graph.leaf_nodes()
        old=targets_before(executor.loader,('games_hub','0025_hubsession_session_type'))
        executor.migrate(old)
        try:
            apps=executor.loader.project_state(old).apps
            User=apps.get_model('auth','User');Hub=apps.get_model('games_hub','HubSession')
            Step=apps.get_model('games_hub','HubGameStep');Party=apps.get_model('games_hub','HubParticipant')
            Quiz=apps.get_model('QuizGame','Quiz');Player=apps.get_model('QuizGame','QuizParticipant')
            host=User.objects.create(username='old-copy-host')
            hub=Hub.objects.create(code='COPY-OLD',creator_id=host.pk,name='Alter Spielabend',games_weight=4)
            game=Quiz.objects.create(creator_id=host.pk,title='Altes Quick Quiz',room_code='COPYQUIZ',status='completed')
            step=Step.objects.create(session_id=hub.pk,game_key='quiz',room_code=game.room_code,order=0,result_snapshot={'existing':7})
            party=Party.objects.create(session_id=hub.pk,nickname='Alpha',score_adjustment=3)
            player=Player.objects.create(quiz_id=game.pk,name='Alpha',hub_session_code=hub.code,total_score=17)
            with tempfile.TemporaryDirectory(prefix='quiz-old-copy-') as folder:
                folder=Path(folder);copy=folder/'upgrade.sqlite3'
                with closing(sqlite3.connect(copy)) as destination:connection.connection.backup(destination)
                (folder/'copy_settings.py').write_text(
                    'from games_website.test_settings import *\nDATABASES["default"]["NAME"] = '+repr(str(copy))+'\n',encoding='utf-8')
                root=Path(__file__).resolve().parents[1]
                env=os.environ.copy();env.update(QUIZ_QA_ROOT=str(folder),PYTHONPATH=str(folder)+os.pathsep+str(root),PYTHONIOENCODING='utf-8')
                result=subprocess.run([sys.executable,'-B','manage.py','migrate','--settings=copy_settings','--noinput'],
                    cwd=root,env=env,text=True,encoding='utf-8',stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=180)
                self.assertEqual(result.returncode,0,result.stdout)
                with closing(sqlite3.connect(copy)) as db:
                    # Django's CHECK constraints use its SQLite functions (e.g.
                    # REGEXP). A bare sqlite3 connection has not registered them.
                    register(db)
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                    self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
                    self.assertEqual(db.execute('SELECT session_type,name,games_weight FROM games_hub_hubsession WHERE id=?',(hub.pk,)).fetchone(),('multiplayer','Alter Spielabend',4))
                    self.assertEqual(db.execute('SELECT game_format,duel_match_config,result_snapshot FROM games_hub_hubgamestep WHERE id=?',(step.pk,)).fetchone(),('standard','{}','{"existing": 7}'))
                    self.assertEqual(db.execute('SELECT total_score FROM QuizGame_quizparticipant WHERE id=?',(player.pk,)).fetchone(),(17,))
                    self.assertEqual(db.execute('SELECT score_adjustment FROM games_hub_hubparticipant WHERE id=?',(party.pk,)).fetchone(),(3,))
                    self.assertIn('target_game_run',{row[0] for row in db.execute('SELECT name FROM sqlite_master WHERE type="table"')})
                self.assertEqual(Player.objects.get(pk=player.pk).total_score,17)
                self.assertNotIn('session_type',{c.name for c in connection.introspection.get_table_description(connection.cursor(),'games_hub_hubsession')})
        finally:MigrationExecutor(connection).migrate(latest)
