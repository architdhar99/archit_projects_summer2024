# -*- coding: utf-8 -*-
"""ai_projects.py

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1sHTFedDCAoTYbJoNCkBVlkbCU0UoJoLX
"""

# Install necessary libraries
!pip install Flask==2.0.3 Werkzeug==2.0.3 Jinja2==3.0.3 gunicorn==20.1.0
!pip install pydub
!pip install --upgrade whisper
!pip install --upgrade git+https://github.com/openai/whisper.git
!pip install google-cloud-translate
!pip install google-cloud-texttospeech
!pip install torch
!pip install flask-ngrok
!pip install jupyter-dash
!pip install pyngrok
!pip install --upgrade pyngrok
!pip install --upgrade flask-ngrok
!apt-get install ffmpeg
!pip install spotdl
!apt-get install ffmpeg
!pip install pyannote.audio nltk
!pip install transformers
!pip install huggingface_hub
!pip install --upgrade pyannote.audio
!pip install celery redis

import os
import shutil
import tempfile
import io
import uuid
import traceback
import logging
from datetime import datetime
from flask import Flask, request, render_template, send_file, jsonify, abort
from pydub import AudioSegment
from pydub.silence import split_on_silence
import whisper
from google.cloud import translate_v2 as translate
from google.cloud import texttospeech
import torch
import nltk
from nltk.tokenize import sent_tokenize
from pyngrok import ngrok
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import subprocess
import threading
import numpy as np
from scipy.io import wavfile
from sklearn.cluster import KMeans
import os
import shutil
import tempfile
import uuid
from flask import send_from_directory
from zipfile import ZipFile

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

nltk.download('punkt')

# Initialize the Flask app
app = Flask(__name__, template_folder='[INSERT HERE]')

# Global variables
LAST_CREATED_FILE = None
TASK_STATUS = {}

# Ngrok setup
ngrok.kill()
!ngrok authtoken [INSERT HERE]
public_url = ngrok.connect(5000)
print(f"Public URL: {public_url}")

# Set the torch hub directory to avoid permission issues
torch.hub.set_dir("./torch_hub")

# Set the environment variable with your Hugging Face token
os.environ["HUGGINGFACE_TOKEN"] = "INSERT HERE"

# Mount Google Drive if not already mounted
from google.colab import drive
drive.mount('/content/drive', force_remount=True)

# Specify the path to your JSON file for Google Cloud credentials
key_file_name = "[INSERT HERE].json"
key_file_path = os.path.join("/content/drive/My Drive/ai_projects", key_file_name)

# Verify the file exists
if not os.path.exists(key_file_path):
    raise FileNotFoundError(f"Service account key file not found at {key_file_path}")
else:
    print(f"Service account key file found at {key_file_path}")

# Set the environment variable for Google Cloud credentials
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = key_file_path

# Check if GPU is available and set the device
device = "cuda" if torch.cuda.is_available() else "cpu"

# Initialize Whisper model
model = whisper.load_model("base", device=device)

# Function for translating text using Google Translate
def translate_text(text, target_language):
    translate_client = translate.Client()
    result = translate_client.translate(text, target_language=target_language)
    return result['translatedText']

# Function to segment text into sentences
def segment_text(text):
    return sent_tokenize(text)

# New simplified diarization function
def simple_diarize_audio(audio_path, num_speakers=2):
    # Load audio file
    audio = AudioSegment.from_wav(audio_path)
    samples = np.array(audio.get_array_of_samples())

    # Normalize audio
    samples = samples / np.max(np.abs(samples))

    # Simple voice activity detection
    energy_threshold = 0.1
    is_speech = np.abs(samples) > energy_threshold

    # Find continuous speech segments
    speech_changes = np.diff(is_speech.astype(int))
    speech_starts = np.where(speech_changes == 1)[0]
    speech_ends = np.where(speech_changes == -1)[0]

    if len(speech_starts) == 0 or len(speech_ends) == 0:
        return [(0, len(samples) / audio.frame_rate, 'SPEAKER_1')]

    if speech_starts[0] > speech_ends[0]:
        speech_starts = np.concatenate(([0], speech_starts))
    if speech_ends[-1] < speech_starts[-1]:
        speech_ends = np.concatenate((speech_ends, [len(samples)]))

    # Extract features (using energy as a simple feature)
    segment_features = []
    for start, end in zip(speech_starts, speech_ends):
        segment = samples[start:end]
        energy = np.mean(segment**2)
        segment_features.append([energy])

    # Cluster segments
    kmeans = KMeans(n_clusters=num_speakers)
    labels = kmeans.fit_predict(segment_features)

    # Create diarization result
    diarization = []
    for (start, end), label in zip(zip(speech_starts, speech_ends), labels):
        diarization.append((start / audio.frame_rate, end / audio.frame_rate, f'SPEAKER_{label+1}'))

    return diarization

# Updated diarize_audio function
def diarize_audio(audio_path):
    try:
        return simple_diarize_audio(audio_path)
    except Exception as e:
        logger.error(f"Error in simple diarization: {str(e)}")
        # Fallback to basic segmentation
        audio = AudioSegment.from_wav(audio_path)
        return [(0, len(audio) / 1000, 'SPEAKER_1')]  # Duration in seconds

# Function for synthesizing speech using Google Cloud TTS and WaveNet
def synthesize_speech(text, language_code, voice, speaking_rate=1.0):
    client = texttospeech.TextToSpeechClient()
    input_text = texttospeech.SynthesisInput(text=text)
    voice_params = texttospeech.VoiceSelectionParams(
        language_code=language_code,
        name=voice,
        ssml_gender=texttospeech.SsmlVoiceGender.NEUTRAL
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=speaking_rate
    )
    response = client.synthesize_speech(
        input=input_text, voice=voice_params, audio_config=audio_config
    )
    return response.audio_content

def process_translation(task_id, video_url, voice):
    global LAST_CREATED_FILE, TASK_STATUS
    temp_dir = tempfile.mkdtemp()
    final_audio_filename = None
    try:
        logger.info(f"Starting translation for task {task_id}")
        TASK_STATUS[task_id] = {'state': 'PROGRESS', 'current': 10, 'total': 100, 'status': 'Downloading audio...'}
        target_language = voice.split('-')[0]
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        audio_filename = os.path.join(temp_dir, "audio.mp3")
        os.system(f'yt-dlp -x --audio-format mp3 -o "{audio_filename}" "{video_url}"')
        if not os.path.exists(audio_filename):
            raise FileNotFoundError(f"{audio_filename} not found")

        TASK_STATUS[task_id] = {'state': 'PROGRESS', 'current': 20, 'total': 100, 'status': 'Converting audio...'}
        wav_filename = os.path.join(temp_dir, "audio.wav")
        os.system(f'ffmpeg -i "{audio_filename}" "{wav_filename}"')

        TASK_STATUS[task_id] = {'state': 'PROGRESS', 'current': 30, 'total': 100, 'status': 'Performing diarization...'}
        diarization = diarize_audio(wav_filename)
        logger.info(f"Diarization result: {diarization}")

        TASK_STATUS[task_id] = {'state': 'PROGRESS', 'current': 50, 'total': 100, 'status': 'Transcribing audio...'}
        result = model.transcribe(wav_filename)

        TASK_STATUS[task_id] = {'state': 'PROGRESS', 'current': 60, 'total': 100, 'status': 'Assigning speakers...'}
        speaker_segments = []
        for segment in result["segments"]:
            start_time = segment["start"]
            end_time = segment["end"]
            text = segment["text"]
            speaker = next((s for s in diarization if s[0] <= start_time < s[1]), ('SPEAKER_1',))[-1]
            speaker_segments.append((speaker, text))

        TASK_STATUS[task_id] = {'state': 'PROGRESS', 'current': 70, 'total': 100, 'status': 'Translating text...'}
        translated_segments = []
        for speaker, text in speaker_segments:
            translated_text = translate_text(text, target_language)
            translated_segments.append((speaker, translated_text))

        TASK_STATUS[task_id] = {'state': 'PROGRESS', 'current': 80, 'total': 100, 'status': 'Synthesizing speech...'}
        audio_segments = []
        voices = [voice, voice.replace('Standard', 'Wavenet')]
        speaker_voice_map = {}
        for speaker, text in translated_segments:
            if speaker not in speaker_voice_map:
                speaker_voice_map[speaker] = voices[len(speaker_voice_map) % len(voices)]
            voice_choice = speaker_voice_map[speaker]
            audio_content = synthesize_speech(text, target_language, voice_choice, speaking_rate=1.2)
            audio_segment = AudioSegment.from_mp3(io.BytesIO(audio_content))
            audio_segments.append(audio_segment)

        TASK_STATUS[task_id] = {'state': 'PROGRESS', 'current': 90, 'total': 100, 'status': 'Finalizing audio...'}
        final_audio = sum(audio_segments)
        final_audio_filename = os.path.join(temp_dir, f"translated_audio_{timestamp}.mp3")
        final_audio.export(final_audio_filename, format="mp3")

        LAST_CREATED_FILE = final_audio_filename
        TASK_STATUS[task_id] = {'state': 'SUCCESS', 'current': 100, 'total': 100, 'status': 'Task completed!', 'result': os.path.basename(final_audio_filename)}
        logger.info(f"Translation completed for task {task_id}")
    except Exception as e:
        logger.error(f"Error in process_translation: {str(e)}")
        logger.error(traceback.format_exc())
        TASK_STATUS[task_id] = {'state': 'FAILURE', 'current': 100, 'total': 100, 'status': f'Task failed: {str(e)}'}
    finally:
        logger.info(f"Cleaning up temporary files for task {task_id}")
        for item in os.listdir(temp_dir):
            item_path = os.path.join(temp_dir, item)
            if item_path != final_audio_filename:
                if os.path.isfile(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)


# Flask routes
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/test', methods=['GET', 'POST'])
def test_route():
    return "Test route is working!", 200

@app.route("/favicon.ico")
def favicon():
    return "", 200

@app.route('/translate', methods=['POST'])
def process_translation_route():
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No JSON data received'}), 400

        video_url = data.get('video_url')
        voice = data.get('voice')

        if not video_url or not voice:
            return jsonify({'error': 'Missing video_url or voice in request data'}), 400

        task_id = str(uuid.uuid4())
        threading.Thread(target=process_translation, args=(task_id, video_url, voice)).start()
        logger.info(f"Started translation task with ID: {task_id}")
        return jsonify({'task_id': task_id}), 202
    except Exception as e:
        logger.error(f"Error in process_translation_route: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/status/<task_id>')
def task_status(task_id):
    status = TASK_STATUS.get(task_id, {'state': 'PENDING', 'current': 0, 'total': 1, 'status': 'Pending...'})
    logger.debug(f"Status for task {task_id}: {status}")
    return jsonify(status)

@app.route('/download/<filename>', methods=['GET'])
def download_file(filename):
    global LAST_CREATED_FILE
    if LAST_CREATED_FILE and os.path.basename(LAST_CREATED_FILE) == filename:
        try:
            return send_file(LAST_CREATED_FILE, as_attachment=True)
        except Exception as e:
            logger.error(f"Error downloading file: {str(e)}")
            abort(500)
    else:
        logger.error(f"File not found: {filename}")
        abort(404)

# Set up Spotify API
client_id = 'INSERT HERE'
client_secret = 'INSERT HERE'
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(client_id=client_id, client_secret=client_secret))

# Global variable to store Spotify download tasks
SPOTIFY_TASKS = {}

import os
import tempfile
import subprocess
from zipfile import ZipFile

def download_spotify_playlist(task_id, playlist_url):
    try:
        output_dir = os.path.join(tempfile.gettempdir(), f'spotify_downloads_{task_id}')
        os.makedirs(output_dir, exist_ok=True)

        SPOTIFY_TASKS[task_id] = {'state': 'PROGRESS', 'status': 'Downloading tracks...'}

        # Split the input in case multiple URLs were provided
        urls = playlist_url.split()

        for url in urls:
            try:
                # Use spotdl to download each track or playlist
                result = subprocess.run(['spotdl', url, '--output', output_dir],
                                        capture_output=True, text=True, check=True)
                print(f"spotdl output for {url}: {result.stdout}")
            except subprocess.CalledProcessError as e:
                print(f"Error downloading {url}: {e}")
                print(f"spotdl error output: {e.output}")

        # Check if any MP3 files were downloaded
        mp3_files = [f for f in os.listdir(output_dir) if f.endswith('.mp3')]

        if not mp3_files:
            raise Exception("No MP3 files were downloaded. Please check the provided URL(s).")

        # Create a zip file containing all downloaded MP3s
        zip_filename = os.path.join(tempfile.gettempdir(), f'spotify_playlist_{task_id}.zip')
        with ZipFile(zip_filename, 'w') as zipf:
            for mp3 in mp3_files:
                zipf.write(os.path.join(output_dir, mp3), mp3)

        SPOTIFY_TASKS[task_id] = {
            'state': 'SUCCESS',
            'status': 'Download complete',
            'zip_file': zip_filename
        }
    except Exception as e:
        SPOTIFY_TASKS[task_id] = {'state': 'FAILURE', 'status': str(e)}
        logger.error(f"Error in download_spotify_playlist: {str(e)}")
        logger.error(traceback.format_exc())

@app.route('/spotify', methods=['POST'])
def spotify_download_route():
    try:
        data = request.json
        if not data or 'playlist_url' not in data:
            return jsonify({'error': 'No playlist URL provided'}), 400

        playlist_url = data['playlist_url']
        task_id = str(uuid.uuid4())

        threading.Thread(target=download_spotify_playlist, args=(task_id, playlist_url)).start()

        return jsonify({'task_id': task_id}), 202
    except Exception as e:
        logger.error(f"Error in spotify_download_route: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/spotify/status/<task_id>')
def spotify_status(task_id):
    status = SPOTIFY_TASKS.get(task_id, {'state': 'PENDING', 'status': 'Task not found'})
    return jsonify(status)

@app.route('/spotify/download/<task_id>')
def spotify_download_file(task_id):
    task = SPOTIFY_TASKS.get(task_id)
    if not task or task['state'] != 'SUCCESS':
        abort(404)

    zip_file = task['zip_file']
    return send_file(zip_file, as_attachment=True, download_name='spotify_playlist.zip')

if __name__ == '__main__':
    app.run(port=5000)

