import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
import threading
import time
import unittest
from unittest.mock import Mock, AsyncMock

from session import run_session


def message(audio=None, interrupted=False, goodbye=False):
    parts = [NS(inline_data=NS(data=audio))] if audio else []
    return NS(go_away=goodbye, server_content=NS(interrupted=interrupted, model_turn=NS(parts=parts)))


class FakeLive:
    def __init__(self, messages=None):
        self.sent=[]
        self.messages=messages
    async def send_realtime_input(self, audio):
        self.sent.append(audio)
    async def receive(self):
        if self.messages is None:
            await asyncio.Event().wait()
        else:
            for msg in self.messages:
                yield msg
                await asyncio.sleep(0.01)
            yield message(goodbye=True)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, live=None, *, hang=False, fail=None):
        self.enter_cancelled=False
        self.exited=False
        @asynccontextmanager
        async def connect(**kwargs):
            try:
                if fail:raise fail
                if hang:await asyncio.Event().wait()
                try:yield live or FakeLive()
                finally:self.exited=True
            except asyncio.CancelledError:
                self.enter_cancelled=True
                raise
        self.client=NS(aio=NS(live=NS(connect=connect),aclose=AsyncMock()),close=Mock())
        return Mock(return_value=self.client)

    async def invoke(self, factory, **overrides):
        self.player=Mock(failed=None)
        self.ready=Mock()
        self.stop=threading.Event()
        self.input=asyncio.Queue()
        settings=dict(api_key='test-key',model='gemini-3.8-live',instruction='test',audio_queue=self.input,
            player=self.player,stop_requested=self.stop,on_ready=self.ready,last_activity=[time.monotonic()],
            connection_timeout=0.1,idle_timeout=0.15,client_factory=factory)
        settings.update(overrides)
        return await run_session(**settings)

    async def test_connection_timeout_cancels_handshake_and_closes_resources(self):
        factory=self.make_client(hang=True)
        with self.assertRaises(TimeoutError):await asyncio.wait_for(self.invoke(factory),1)
        self.assertTrue(self.enter_cancelled)
        self.ready.assert_not_called()
        self.player.close.assert_called_once()
        self.client.close.assert_called_once()
        self.client.aio.aclose.assert_awaited_once()

    async def test_stop_works_while_connecting(self):
        factory=self.make_client(hang=True)
        async def stop():
            await asyncio.sleep(0.02)
            self.stop.set()
        task=asyncio.create_task(stop())
        await asyncio.wait_for(self.invoke(factory,connection_timeout=10),1)
        await task
        self.assertTrue(self.enter_cancelled)
        self.player.close.assert_called_once()

    async def test_idle_conversation_disconnects_and_does_not_retry_google(self):
        factory=self.make_client()
        await asyncio.wait_for(self.invoke(factory),1)
        self.ready.assert_called_once()
        factory.assert_called_once()
        self.assertTrue(self.exited)
        self.player.close.assert_called_once()

    async def test_streams_cleaned_microphone_and_plays_model_audio(self):
        live=FakeLive([message(audio=b'model'),message(interrupted=True)])
        queue=asyncio.Queue();queue.put_nowait(b'cleaned mic')
        await self.invoke(self.make_client(live),audio_queue=queue)
        self.assertEqual(b'cleaned mic',live.sent[0].data)
        self.assertEqual('audio/pcm;rate=16000',live.sent[0].mime_type)
        self.player.play.assert_called_once_with(b'model')
        self.player.clear.assert_called_once()
        self.assertTrue(self.exited)

    async def test_network_error_propagates_for_tray_reporting_and_closes_client(self):
        with self.assertRaisesRegex(RuntimeError,'network failure'):
            await self.invoke(self.make_client(fail=RuntimeError('network failure')))
        self.ready.assert_not_called()
        self.player.close.assert_called_once()
        self.client.close.assert_called_once()

    async def test_output_failure_ends_session_instead_of_leaving_a_live_icon(self):
        player=Mock(failed=RuntimeError('sink failed'))
        with self.assertRaisesRegex(RuntimeError,'پخش صدا'):
            await self.invoke(self.make_client(),player=player)
        player.close.assert_called_once()
