import sys
import os
import numpy as np
import librosa
from joblib import load
import sounddevice as sd
import queue
import time
import soundfile as sf
          
SAMPLE_RATE = 22050
DURATION = 3  # seconds per chunk
N_MFCC = 13
MODEL_PATH = 'ambulance_sound_model.joblib'
THRESHOLD = 0.9  # Confidence threshold for ambulance detection          

q = queue.Queue()

def extract_features_from_audio(audio, sample_rate=SAMPLE_RATE, n_mfcc=N_MFCC):
    mfcc = librosa.feature.mfcc(y=audio, sr=sample_rate, n_mfcc=n_mfcc)
    mfcc_mean = np.mean(mfcc.T, axis=0)                     
    return mfcc_mean

def extract_features_from_file(file_path, n_mfcc=N_MFCC, sample_rate=SAMPLE_RATE):              
    y, sr = librosa.load(file_path, sr=sample_rate)
    return extract_features_from_audio(y, sample_rate, n_mfcc)                     

def predict(features, clf):
    features = features.reshape(1, -1)
    proba = clf.predict_proba(features)[0]
    if proba[1] >= THRESHOLD:
        print(f"AMBULANCE DETECTED! Confidence: {proba[1]:.2f}")
    else:
        print(f"No ambulance. Confidence: {proba[1]:.2f}")

def mic_mode():
    print("Available input devices:")
    devices = sd.query_devices()
    input_devices = [(i, d['name']) for i, d in enumerate(devices) if d['max_input_channels'] > 0]
    for idx, name in input_devices:
        print(f"{idx}: {name}")
    device_idx = int(input("Enter device index to use for microphone: "))
    print("Listening for ambulance sounds... Press Ctrl+C to stop.")
    os.makedirs("detected_audio", exist_ok=True)
    chunk_count = 0
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, device=device_idx, callback=lambda indata, frames, time_info, status: q.put(indata.copy()), blocksize=int(SAMPLE_RATE * DURATION)):
        try:
            while True:
                audio_chunk = q.get().flatten()
                # Save every audio chunk
                filename = f"detected_audio/chunk_{chunk_count}_{int(time.time())}.wav"
                sf.write(filename, audio_chunk, SAMPLE_RATE)
                print(f"Saved audio chunk to {filename}")
                features = extract_features_from_audio(audio_chunk)
                features_reshaped = features.reshape(1, -1)
                proba = clf.predict_proba(features_reshaped)[0]
                if proba[1] >= THRESHOLD:
                    print(f"AMBULANCE DETECTED! Confidence: {proba[1]:.2f}")
                else:
                    print(f"No ambulance. Confidence: {proba[1]:.2f}")
                chunk_count += 1
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nStopped listening.")

def file_mode(audio_path):
    if not os.path.exists(audio_path):
        print(f'Audio file {audio_path} not found.')
        sys.exit(1)
    features = extract_features_from_file(audio_path)
    predict(features, clf)

def detect_ambulance_siren(audio_path):
    """
    Returns True if ambulance siren is detected in the audio file, else False.
    Uses a simple frequency domain energy check as a placeholder.
    """
    try:
        y, sr = librosa.load(audio_path, sr=None, mono=True)
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
        # Find indices for siren frequency range
        SIREN_FREQ_RANGE = (600, 1800)
        idx = np.where((freqs >= SIREN_FREQ_RANGE[0]) & (freqs <= SIREN_FREQ_RANGE[1]))[0]
        # Compute mean energy in siren band
        siren_band_energy = np.mean(S[idx, :]) if len(idx) > 0 else 0
        total_energy = np.mean(S)
        # Heuristic: siren band must be at least 2x stronger than average
        if siren_band_energy > 2 * total_energy:
            return True
        return False
    except Exception as e:
        print(f"Ambulance siren detection failed: {e}")
        return False

def usage():
    print("Usage:")
    print("  python detect_ambulance.py mic         # Real-time detection from microphone")
    print("  python detect_ambulance.py file path_to_audio.wav   # Detect from audio file")

if __name__ == "__main__":
    if not os.path.exists(MODEL_PATH):
        print(f'Model file {MODEL_PATH} not found. Please train the model first.')
        sys.exit(1)
    clf = load(MODEL_PATH)
    if len(sys.argv) == 2 and sys.argv[1] == 'mic':
        mic_mode()
    elif len(sys.argv) == 3 and sys.argv[1] == 'file':
        file_mode(sys.argv[2])
    else:
        usage()