"""Read-only live-server diagnostics; safe to import before Django setup on Windows."""
import time
import json
from asgiref.sync import SyncToAsync
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler


class ObservedStaticFilesHandler(ASGIStaticFilesHandler):
    def __init__(self,application):
        super().__init__(application)
        from django.db import connections
        from target_game import views
        original=views.snapshot
        def profiled(*args,**kwargs):
            started=time.monotonic();queries=[];db=connections['default']
            def observe(execute,sql,params,many,context):
                before=time.monotonic()
                try:return execute(sql,params,many,context)
                finally:queries.append((time.monotonic()-before,sql[:95]))
            try:
                with db.execute_wrapper(observe):return original(*args,**kwargs)
            finally:
                elapsed=time.monotonic()-started
                if elapsed>1:
                    print('Evening25 snapshot profile',args[1] if len(args)>1 else '',round(elapsed,2),
                          'queries',len(queries),'SQL seconds',round(sum(q[0] for q in queries),2),
                          'slowest',[(round(t,2),sql) for t,sql in sorted(queries,reverse=True)[:2]],flush=True)
        views.snapshot=profiled

    async def __call__(self, scope, receive, send):
        started=time.monotonic()
        async def observed_send(message):
            if message['type']=='websocket.accept' and time.monotonic()-started>1:
                print('Evening25 slow websocket accept',scope['path'],round(time.monotonic()-started,2),flush=True)
            await send(message)
        async def observed_receive():
            message=await receive()
            if message['type']=='websocket.receive':
                try:
                    action=json.loads(message.get('text','{}')).get('type','')
                    if action.startswith('admin_'):
                        print('Evening25 server received',action,round(time.monotonic()-started,2),'db queue',SyncToAsync.single_thread_executor._work_queue.qsize(),flush=True)
                except ValueError:
                    pass
            if message['type']=='websocket.disconnect' and message.get('code') not in (1000,1001):
                print('Evening25 server disconnect',scope['path'],message.get('code'),round(time.monotonic()-started,2),flush=True)
            return message
        try:
            await super().__call__(scope,observed_receive,observed_send)
        finally:
            elapsed=time.monotonic()-started
            if scope['type']=='http' and elapsed>2:
                print('Evening25 slow HTTP',scope.get('method'),scope['path'],round(elapsed,2),'db queue',SyncToAsync.single_thread_executor._work_queue.qsize(),flush=True)
