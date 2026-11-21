"""A bounded Gemini connection/session lifecycle, independent of GTK."""
import asyncio
import contextlib
import time


async def run_session(*, api_key, model, instruction, audio_queue, player,
                      stop_requested, on_ready, last_activity,
                      connection_timeout=20, idle_timeout=60, client_factory=None):
    from google import genai
    from google.genai import types
    client_factory = client_factory or genai.Client
    client = None
    connected = False
    started = time.monotonic()
    tasks = []
    try:
        client = client_factory(api_key=api_key, http_options={'api_version': 'v1beta'})
        config = types.LiveConnectConfig(response_modalities=['AUDIO'], system_instruction=instruction)

        async def conversation():
            nonlocal connected
            async with client.aio.live.connect(model=model, config=config) as live:
                if stop_requested.is_set():
                    return
                connected = True
                last_activity[0] = time.monotonic()
                on_ready()

                async def send():
                    while not stop_requested.is_set():
                        pcm = await audio_queue.get()
                        await live.send_realtime_input(audio=types.Blob(data=pcm, mime_type='audio/pcm;rate=16000'))

                async def receive():
                    while not stop_requested.is_set():
                        received = False
                        async for message in live.receive():
                            received = True
                            if message.go_away:
                                return
                            content = message.server_content
                            if not content:
                                continue
                            if content.interrupted:
                                player.clear()
                            for part in content.model_turn.parts if content.model_turn else ():
                                if part.inline_data and part.inline_data.data:
                                    await asyncio.to_thread(player.play, part.inline_data.data)
                                    last_activity[0] = time.monotonic()
                        if not received:
                            return

                streams = [asyncio.create_task(send()), asyncio.create_task(receive())]
                try:
                    done, _ = await asyncio.wait(streams, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in streams:
                        task.cancel()
                    await asyncio.gather(*streams, return_exceptions=True)

        async def guard():
            while not stop_requested.is_set():
                if player.failed:
                    raise RuntimeError('پخش صدا در دسترس نیست.') from player.failed
                if not connected and time.monotonic() - started >= connection_timeout:
                    raise TimeoutError('اتصال به گوگل در مهلت تعیین‌شده برقرار نشد؛ پراکسی یا شبکه را بررسی کنید.')
                if connected and time.monotonic() - last_activity[0] >= idle_timeout:
                    return
                await asyncio.sleep(0.1)

        tasks = [asyncio.create_task(conversation()), asyncio.create_task(guard())]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        player.close()
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.aio.aclose(), timeout=3)
            with contextlib.suppress(Exception):
                client.close()
