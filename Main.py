import discord
from discord.ext import commands
from discord.ext.voice_recv import VoiceRecvClient
from discord.ext.voice_recv.sinks import WaveSink
import asyncio
import os
import speech_recognition as sr
import io
import re
from pydub import AudioSegment
import json
from datetime import datetime
from groq import Groq

TOKEN = "MTM4Njc0NzQyNTQwNjA2MjYwMg.GJLaXD.vzIguIjRcPEox2xhxKttL0M1SDKsah7f79a2Sg"

TEMP_AUDIO_DIR = "temp_audio"
KEYWORD_RESPONSES_FILE = "keyword_responses.json"

# --- Konfiguracja Bota Discord ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Globalna zmienna do przechowywania par słowo kluczowe-odpowiedź
keyword_responses = []

# --- GLOBALNA ZMIENNA DO OBSŁUGI ZAPYTAŃ AI ---
user_ai_query_state = {}


# --- Funkcje Pomocnicze ---
def load_keyword_responses(file_path):
    # Ładuje pary słowo kluczowe-odpowiedź z określonego pliku JSON.
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and all(
                        isinstance(item, dict) and "keywords" in item and "response" in item and isinstance(
                                item["response"], dict) for item in data):
                    print(f"Załadowano {len(data)} par słowo kluczowe-odpowiedź z {file_path}")
                    return data
                else:
                    print(
                        f"Ostrzeżenie: Plik {file_path} ma nieprawidłowy format. Oczekiwano listy obiektów z kluczami 'keywords' i słownikiem 'response' zawierającym 'text' i/lub 'image_path'. Ładowanie pustej listy.")
                    return []
        except json.JSONDecodeError as e:
            print(f"Błąd dekodowania JSON z {file_path}: {e}. Ładowanie pustej listy.")
            return []
        except Exception as e:
            print(f"Wystąpił błąd podczas ładowania {file_path}: {e}. Ładowanie pustej listy.")
            return []
    else:
        print(f"Plik odpowiedzi ze słowami kluczowymi '{file_path}' nie znaleziony. Inicjowanie bez odpowiedzi.")
        return []


# --- Niestandardowy WaveSink do filtrowania botów ---
class MyCustomWaveSink(WaveSink):

    def __init__(self, output_buffer, bot_user_id):
        super().__init__(output_buffer)
        self.bot_user_id = bot_user_id  # ID samego bota

    def write(self, user, data):
        # Ignoruj audio, jeśli użytkownik jest botem i nie jest to ten bot
        if user.bot and user.id != self.bot_user_id:
            return
        # Jeśli nie jest botem, lub jest to audio tego bota, kontynuuj zapis
        super().write(user, data)


# --- Logika przetwarzania audio ---
async def process_audio_finished(audio_buffer_io: io.BytesIO, ctx, voice_client_instance=None,
                                 is_final_processing=False):
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    audio_buffer_io.seek(0)

    current_segment = AudioSegment.empty()
    try:
        current_segment = AudioSegment.from_wav(audio_buffer_io)
    except Exception as e:
        print(f"Błąd dekodowania audio za pomocą pydub (bieżący segment): {e}. Plik może być pusty lub uszkodzony.")
        return

    segment_to_recognize_full = current_segment
    overlap_duration_ms = 2000  # 2 sekundy nakładki

    overlap_segment_to_process = AudioSegment.empty()
    if voice_client_instance and hasattr(voice_client_instance,
                                         'overlap_segment') and voice_client_instance.overlap_segment and len(
        voice_client_instance.overlap_segment) > 0:
        overlap_segment_to_process = voice_client_instance.overlap_segment
        segment_to_recognize_full = overlap_segment_to_process + current_segment
        print(f"Utworzono segment do rozpoznawania z nakładką. Długość: {len(segment_to_recognize_full)} ms.")
    else:
        print(f"Przetwarzanie segmentu bez nakładki. Długość: {len(segment_to_recognize_full)} ms.")

    recognizer = sr.Recognizer()

    # Zdefiniuj słowa kluczowe dla wariantów komendy "bot leave"
    leave_command_keywords = ["bot leave", "bot live", "bot leaf", "leave bot", "leaf bot", "live bot", "boot leave",
                              "bot lif", "wyjdź"]

    full_recognized_text = ""
    # Utwórz tymczasową nazwę pliku dla audio do rozpoznania
    full_audio_filename = os.path.join(TEMP_AUDIO_DIR,
                                       f"full_audio_{ctx.guild.id}_{ctx.author.id}_{asyncio.get_event_loop().time()}.wav")
    try:
        # Eksportuj połączony segment audio do pliku WAV
        segment_to_recognize_full.export(full_audio_filename, format="wav")
        with sr.AudioFile(full_audio_filename) as source:
            audio = recognizer.record(source)
            full_recognized_text = recognizer.recognize_google(audio, language="pl-PL").lower()
            print(f"[Rozpoznany pełny tekst audio]: {full_recognized_text}")
    except sr.UnknownValueError:
        print(f"[Rozpoznany pełny tekst audio]: Nie można zrozumieć audio.")
    except sr.RequestError as e:
        print(f"[Rozpoznany pełny tekst audio]: Błąd usługi rozpoznawania mowy: {e}")
    except Exception as e:
        print(f"Wystąpił błąd podczas eksportowania/rozpoznawania pełnego audio: {e}")
    finally:
        if os.path.exists(full_audio_filename):
            try:
                os.remove(full_audio_filename)
            except Exception as e:
                print(f"Błąd podczas usuwania pliku {full_audio_filename}: {e}")

    overlap_recognized_text = ""
    if not is_final_processing and overlap_segment_to_process and len(overlap_segment_to_process) > 0:
        overlap_audio_filename = os.path.join(TEMP_AUDIO_DIR,
                                              f"overlap_audio_{ctx.guild.id}_{ctx.author.id}_{asyncio.get_event_loop().time()}.wav")
        try:
            overlap_segment_to_process.export(overlap_audio_filename, format="wav")
            with sr.AudioFile(overlap_audio_filename) as source:
                audio = recognizer.record(source)
                overlap_recognized_text = recognizer.recognize_google(audio, language="pl-PL").lower()
                print(f"[Rozpoznany tekst nakładki audio]: {overlap_recognized_text}")
        except sr.UnknownValueError:
            print(f"[Rozpoznany tekst nakładki audio]: Nie można zrozumieć audio.")
        except sr.RequestError as e:
            print(f"[Rozpoznany tekst nakładki audio]: Błąd usługi rozpoznawania mowy: {e}")
        except Exception as e:
            print(f"Wystąpił błąd podczas eksportowania/rozpoznawania nakładki audio: {e}")
        finally:
            if os.path.exists(overlap_audio_filename):
                try:
                    os.remove(overlap_audio_filename)
                except Exception as e:
                    print(f"Błąd podczas usuwania pliku {overlap_audio_filename}: {e}")

    # Określ czego nie było w nakładce
    new_text_segment = ""
    if full_recognized_text and overlap_recognized_text:
        if full_recognized_text.startswith(overlap_recognized_text):
            new_text_segment = full_recognized_text[len(overlap_recognized_text):].strip()
        else:
            new_text_segment = full_recognized_text
    else:
        new_text_segment = full_recognized_text

    # Pobierz stan zapytania AI dla bieżącego użytkownika
    user_id = ctx.author.id
    current_ai_state = user_ai_query_state.get(user_id, {"active": False, "query_text": ""})

    # ---Rozpoznawanie ZAPYTAJ/STOP ---
    if current_ai_state["active"]:
        current_ai_state["query_text"] += " " + new_text_segment

        stop_match = re.search(r"(.*?)stop", current_ai_state["query_text"], re.IGNORECASE)
        if stop_match:
            query_content = stop_match.group(1).strip()
            print(f"Wykryto zakończenie zapytania do AI. Pełna treść: '{query_content}'")
            await ctx.send(f"Rozpoznałem pełne zapytanie. Przetwarzam: '{query_content}'...")

            user_ai_query_state[user_id] = {"active": False, "query_text": ""}

            if query_content:
                try:
                    ai_response = await ask_AI(query_content)
                    await ctx.send(f"Odpowiedź AI: {ai_response}")
                except Exception as e:
                    await ctx.send(f"Przepraszam, wystąpił błąd podczas uzyskiwania odpowiedzi od AI: {e}")
                return
            else:
                await ctx.send("Wykryłem 'ZAPYTAJ:' i 'STOP', ale nie ma treści zapytania. Spróbuj ponownie.")
                return

    else:
        start_ai_query_match = re.search(r"zapytaj\s*(.*)", new_text_segment, re.IGNORECASE)
        if start_ai_query_match:
            initial_content = start_ai_query_match.group(1).strip()
            user_ai_query_state[user_id] = {"active": True, "query_text": initial_content}
            print(f"Rozpoczęto zapytanie do AI. Początkowa treść: '{initial_content}'")
            await ctx.send("Zaczynam słuchać Twojego zapytania do AI. Powiedz 'STOP' gdy skończysz.")

            stop_match_initial = re.search(r"(.*?)stop", initial_content, re.IGNORECASE)
            if stop_match_initial:
                query_content = stop_match_initial.group(1).strip()
                print(f"Wykryto zakończenie zapytania do AI już w pierwszym fragmencie. Pełna treść: '{query_content}'")
                await ctx.send(f"Rozpoznałem pełne zapytanie. Przetwarzam: '{query_content}'...")

                user_ai_query_state[user_id] = {"active": False, "query_text": ""}

                if query_content:
                    try:
                        ai_response = await ask_AI(query_content)
                        await ctx.send(f"Odpowiedź AI: {ai_response}")
                    except Exception as e:
                        await ctx.send(f"Przepraszam, wystąpił błąd podczas uzyskiwania odpowiedzi od AI: {e}")
                    return
                else:
                    await ctx.send("Wykryłem 'ZAPYTAJ:' i 'STOP', ale nie ma treści zapytania. Spróbuj ponownie.")
                    return

            return

    # --- Logika komend ---
    for keyword_phrase in leave_command_keywords:
        if keyword_phrase in new_text_segment:
            print(f"Wykryto '{keyword_phrase}'. Wywoływanie komendy opuszczenia.")
            if user_id in user_ai_query_state:
                del user_ai_query_state[user_id]
            await ctx.invoke(bot.get_command('leave'))
            return

    # --- Logika niestandardowych odpowiedzi na słowa kluczowe ---
    if not current_ai_state["active"]:
        for item in keyword_responses:
            keywords_to_check = [kw.lower() for kw in item.get("keywords", [])]
            response_content = item.get("response", {})

            if not keywords_to_check or not response_content:
                continue

            for kw in keywords_to_check:
                if kw in new_text_segment:
                    text_to_send = response_content.get("text")
                    image_path_to_send = response_content.get("image_path")

                    if kw == "time" or any(s in new_text_segment for s in ["time", "godzina", "która godzina"]):
                        current_time = datetime.now().strftime("%H:%M:%S %p %Z")
                        if text_to_send:
                            if "{time}" in text_to_send:
                                text_to_send = text_to_send.replace("{time}", current_time)
                            else:
                                text_to_send = f"{text_to_send} Aktualny czas to: {current_time}."
                        else:
                            text_to_send = f"Aktualny czas to: {current_time}."

                    files_to_send = []
                    if image_path_to_send:
                        if os.path.exists(image_path_to_send):
                            files_to_send.append(discord.File(image_path_to_send))
                            print(f"Przygotowywanie do wysłania obrazu: {image_path_to_send}")
                        else:
                            print(f"Ostrzeżenie: Plik obrazu nie znaleziony pod ścieżką '{image_path_to_send}'.")

                    send_kwargs = {}
                    if text_to_send:
                        send_kwargs["content"] = text_to_send
                    if files_to_send:
                        send_kwargs["files"] = files_to_send

                    if send_kwargs:
                        print(
                            f"Wykryto niestandardowe słowo kluczowe '{kw}'. Wysyłanie odpowiedzi: Tekst='{text_to_send}', Obraz='{image_path_to_send}'")
                        await ctx.send(**send_kwargs)
                    else:
                        print(
                            f"Wykryto niestandardowe słowo kluczowe '{kw}', ale nie ma tekstu ani obrazu do wysłania.")

                    break

    # --- Aktualizacja segmentu nakładki dla następnej iteracji ---
    if voice_client_instance and not is_final_processing:
        if len(current_segment) > overlap_duration_ms:
            voice_client_instance.overlap_segment = current_segment[-overlap_duration_ms:]
        else:
            voice_client_instance.overlap_segment = current_segment
        print(f"Zaktualizowano segment nakładki. Długość nakładki: {len(voice_client_instance.overlap_segment)} ms.")
    elif voice_client_instance and hasattr(voice_client_instance, 'overlap_segment'):
        voice_client_instance.overlap_segment = AudioSegment.empty()
        print("Wyczyszczono segment nakładki (bot przestał nasłuchiwać lub końcowe przetwarzanie).")


# Zapytanie do AI

async def ask_AI(context):
    question = context
    client = Groq(

        api_key="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",

    )
    chat_completion = client.chat.completions.create(
        messages=[
            {
                "role": "user",
                "content": question,
            }
        ],
        model="llama-3.3-70b-versatile",
    )
    return (chat_completion.choices[0].message.content)


# --- Zdarzenia Bota ---
@bot.event
async def on_ready():
    global keyword_responses
    keyword_responses = load_keyword_responses(KEYWORD_RESPONSES_FILE)  # Załaduj odpowiedzi przy uruchomieniu
    print(f"{bot.user} jest online! Nasłuchiwanie komend głosowych i niestandardowych słów kluczowych.")


# --- Komendy Bota ---
@bot.command()
async def join(ctx):
    # Sprawia, że bot dołącza do kanału głosowego i rozpoczyna nasłuchiwanie.
    if not ctx.author.voice:
        return await ctx.send("Musisz najpierw dołączyć do kanału głosowego, aby bot mógł nasłuchiwać.")

    channel = ctx.author.voice.channel
    try:
        vc = await channel.connect(cls=VoiceRecvClient)
        await ctx.send(f"Dołączyłem do kanału głosowego: **{channel.name}**!")

        vc.current_audio_buffer = io.BytesIO()
        vc.current_sink = MyCustomWaveSink(vc.current_audio_buffer, bot.user.id)
        vc.overlap_segment = AudioSegment.empty()

        vc.listen(vc.current_sink)

        await ctx.send(
            "Rozpocząłem nasłuchiwanie komend głosowych i niestandardowych słów kluczowych. Wyniki będą pojawiać się co 5 sekund.")

        async def process_loop():
            while vc.is_listening():
                await asyncio.sleep(5)  # Długość fragmentu

                if not vc.is_listening() or not vc.channel:
                    print("Przerwano nasłuchiwanie lub opuszczono kanał głosowy, zatrzymywanie pętli.")
                    break

                vc.stop_listening()

                if vc.current_audio_buffer.tell() > 0:
                    audio_for_processing = io.BytesIO(vc.current_audio_buffer.getvalue())
                    await process_audio_finished(audio_for_processing, ctx, vc)
                else:
                    print("Bot: Bieżący bufor audio jest pusty. Nie przetwarzam.")

                vc.current_audio_buffer = io.BytesIO()
                vc.current_sink = MyCustomWaveSink(vc.current_audio_buffer, bot.user.id)
                vc.listen(vc.current_sink)
                print("Wznowiono nasłuchiwanie.")

        bot.loop.create_task(process_loop())

    except asyncio.TimeoutError:
        await ctx.send("Nie udało się połączyć z kanałem głosowym. Przekroczono czas. ")
    except discord.ClientException as e:
        await ctx.send(f"Wystąpił błąd podczas łączenia z głosem: {e} ")
    except Exception as e:
        await ctx.send(f"Wystąpił nieoczekiwany błąd w komendzie dołączania: {e} ")


@bot.command()
async def leave(ctx):
    # Sprawia, że bot opuszcza bieżący kanał głosowy.
    if ctx.voice_client:
        if isinstance(ctx.voice_client, VoiceRecvClient) and ctx.voice_client.is_listening():
            ctx.voice_client.stop_listening()
            if hasattr(ctx.voice_client, 'current_audio_buffer') and ctx.voice_client.current_audio_buffer.tell() > 0:
                await ctx.send("Przetwarzam końcowe audio przed wyjściem... ")
                vc_temp = ctx.voice_client
                vc_temp.overlap_segment = AudioSegment.empty()
                audio_for_processing = io.BytesIO(vc_temp.current_audio_buffer.getvalue())
                await process_audio_finished(audio_for_processing, ctx, vc_temp, is_final_processing=True)

                del ctx.voice_client.current_audio_buffer
                if hasattr(ctx.voice_client, 'current_sink'):
                    del ctx.voice_client.current_sink
                if hasattr(ctx.voice_client, 'overlap_segment'):
                    ctx.voice_client.overlap_segment = AudioSegment.empty()

        user_id = ctx.author.id
        if user_id in user_ai_query_state:
            del user_ai_query_state[user_id]

        await ctx.voice_client.disconnect()
        await ctx.send("Opuściłem kanał głosowy. Do zobaczenia!")
    else:
        await ctx.send("Nie jestem na żadnym kanale głosowym.")


# Uruchom bota z określonym tokenem
bot.run(TOKEN)