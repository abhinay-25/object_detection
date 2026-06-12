import cv2
import numpy as np
from ultralytics import YOLO
import time
from flask import Flask, render_template, Response, request, redirect, url_for, send_from_directory
import firebase_admin
from firebase_admin import credentials, db    
import os
import tempfile
import pickle
from collections import deque
from ambsound import detect_ambulance_siren
import threading

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Initialize YOLOv8 model (vehicle detection)
model = YOLO('yolov8n.pt')  # Nano model for speed     

# Initialize Firebase only once      
def init_firebase():     
    if not firebase_admin._apps:
        firebase_key_json = os.environ.get('FIREBASE_KEY_JSON')      
        if not firebase_key_json:           
            # Fallback: try to read from file
            key_path = os.path.join(os.path.dirname(__file__), 'FIREBASE_KEY_JSON')
            if os.path.exists(key_path):
                with open(key_path, 'r') as f:
                    firebase_key_json = f.read()
            else:
                raise RuntimeError("FIREBASE_KEY_JSON environment variable not set and FIREBASE_KEY_JSON file not found.")
        # Write the JSON key to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, mode='w', suffix='.json') as temp_key_file:    
            temp_key_file.write(firebase_key_json)     
            temp_key_path = temp_key_file.name
        cred = credentials.Certificate(temp_key_path)
        firebase_admin.initialize_app(cred, {        
            'databaseURL': 'https://hackathonproject-8b9db-default-rtdb.asia-southeast1.firebasedatabase.app/'      
        })
                
init_firebase()           

class ETAEstimator:
    def __init__(self, model_path=None):
        self.model = None
        try:
            if model_path:
                with open(model_path, 'rb') as f:
                    self.model = pickle.load(f)
        except Exception as e:
            print(f"ETAAstimator: Failed to load model: {e}. Using fallback rules.")

    def predict_eta(self, vehicle_count_last_30s, traffic_density):
        # Fallback logic if model is not loaded
        if self.model is None:
            if traffic_density == 'High':
                return 15
            elif traffic_density == 'Medium':
                return 45
            else:
                return 60
        try:
            density_map = {'Low': 0, 'Medium': 1, 'High': 2}
            X = [[vehicle_count_last_30s, density_map.get(traffic_density, 0)]]
            return int(self.model.predict(X)[0])
        except Exception as e:
            print(f"ETAAstimator: Model prediction failed: {e}")
            return None

eta_estimator = ETAEstimator()

class TrafficSystem:
    def __init__(self, lane_id=1):
        self.vehicle_count = 0
        self.last_update = time.time()
        self.update_interval = 5  # seconds
        self.traffic_density = "Low"
        self.lane_id = lane_id
        self.last_vehicle_count = 0
        self.last_density = "Low"
        self.last_congestion_length = 0
        # ETA additions
        self.recent_counts = deque()  # stores (timestamp, vehicle_count)
        self.last_eta = None
        self.last_ambulance = False  # Store ambulance detection for this lane
    
    def process_frame(self, frame, run_detection=True):
        now = time.time()
        if run_detection:
            results = model(frame)
            vehicle_classes = [2, 3, 5, 7]
            current_count = 0
            frame_height = frame.shape[0]
            min_y = frame_height
            max_y = 0
            for result in results:
                boxes = result.boxes
                for box in boxes:
                    if int(box.cls) in vehicle_classes:
                        current_count += 1
                        x1, y1, x2, y2 = map(int, box.xyxy[0])
                        min_y = min(min_y, y1)
                        max_y = max(max_y, y2)
                        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            # --- Congestion length calculation (normalized 1-100) ---
            if current_count > 0:
                raw_congestion = max_y - min_y
                # Normalize: more vehicles = higher congestion, but always 1-100
                congestion_length = int(np.clip((current_count / 20) * 100, 1, 100))
            else:
                congestion_length = 1
            # --- ETA: update recent_counts ---
            self.recent_counts.append((now, current_count))
            # Remove counts older than 30 seconds
            while self.recent_counts and now - self.recent_counts[0][0] > 30:
                self.recent_counts.popleft()
            if now - self.last_update >= self.update_interval:
                self.vehicle_count = current_count
                self.last_update = now
                if self.vehicle_count > 20:
                    self.traffic_density = "High"
                elif self.vehicle_count > 10:
                    self.traffic_density = "Medium"
                else:
                    self.traffic_density = "Low"
                # --- ETA: predict every 30s ---
                if int(now) % 30 < self.update_interval:  # aligns with 30s window
                    vehicle_count_30s = sum(c for t, c in self.recent_counts)
                    base_eta = eta_estimator.predict_eta(vehicle_count_30s, self.traffic_density)
                    # Apply ambulance ETA multiplier
                    multiplier = lane_eta_multiplier[self.lane_id-1]
                    if lane_ambulance_present[self.lane_id-1]:
                        self.last_eta = max(5, int(base_eta * multiplier))
                    else:
                        self.last_eta = base_eta
                # --- Ambulance presence ---
                if hasattr(self, 'lane_id'):
                    self.last_ambulance = lane_ambulance_present[self.lane_id-1]
                # Send live data to Firebase for this lane
                data = {
                    'vehicle_count': self.vehicle_count,
                    'traffic_density': self.traffic_density,
                    'congestion_length': congestion_length,
                    'timestamp': int(now),
                    'estimated_eta': self.last_eta if self.last_eta is not None else 'N/A',
                    'ambulance_present': self.last_ambulance,
                }
                db.reference(f'traffic_data/lane{self.lane_id}').set(data)
            # Update last detection results
            self.last_vehicle_count = self.vehicle_count
            self.last_density = self.traffic_density
            self.last_congestion_length = congestion_length
        # Always draw overlay with last known results
        annotated_frame = frame.copy()
        cv2.putText(annotated_frame, f"Lane {self.lane_id} Vehicles: {self.last_vehicle_count}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        cv2.putText(annotated_frame, f"Traffic: {self.last_density}", (10, 70),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        cv2.putText(annotated_frame, f"Congestion Length: {self.last_congestion_length}", (10, 110),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        # Overlay ETA
        eta_str = str(self.last_eta) + ' sec' if self.last_eta is not None else 'N/A'
        cv2.putText(annotated_frame, f"Estimated ETA: {eta_str}", (10, 150),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 2)
        # Overlay ambulance presence
        if self.last_ambulance:
            cv2.putText(annotated_frame, "Ambulance Detected!", (10, 200), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
        return annotated_frame

# --- Remove YouTube/yt_dlp logic and LANE_URLS ---
LANE_COUNT = 4
lane_systems = [TrafficSystem(lane_id=i+1) for i in range(LANE_COUNT)]
lane_caps = [None for _ in range(LANE_COUNT)]
lane_video_paths = [os.path.join(app.config['UPLOAD_FOLDER'], f'lane{i+1}.mp4') for i in range(LANE_COUNT)]
lane_audio_paths = [os.path.join(app.config['UPLOAD_FOLDER'], f'lane{i+1}.mp3') for i in range(LANE_COUNT)]
lane_ambulance_present = [False for _ in range(LANE_COUNT)]  # Store ambulance detection per lane
lane_ambulance_last_time = [0 for _ in range(LANE_COUNT)]    # Store last detection time per lane
lane_eta_multiplier = [1 for _ in range(LANE_COUNT)]  # 1 if normal, 1/3 if ambulance detected

AMBULANCE_TIMEOUT = 15  # seconds

def ambulance_timeout_thread():
    while True:
        now = time.time()
        for i in range(LANE_COUNT):
            if lane_ambulance_present[i]:
                if now - lane_ambulance_last_time[i] > AMBULANCE_TIMEOUT:
                    lane_ambulance_present[i] = False
                    lane_eta_multiplier[i] = 1  # Reset ETA multiplier
                    db.reference(f'traffic_data/lane{i+1}').update({'ambulance_present': False})
            else:
                lane_eta_multiplier[i] = 1  # Always ensure normal ETA if not present
        time.sleep(1)

threading.Thread(target=ambulance_timeout_thread, daemon=True).start()

@app.route('/upload/<int:lane_id>', methods=['POST'])
def upload_lane(lane_id):
    if not (1 <= lane_id <= LANE_COUNT):
        return "Invalid lane", 400
    video = request.files.get('video')
    audio = request.files.get('audio')
    video_path = lane_video_paths[lane_id-1]
    audio_path_mp3 = os.path.splitext(lane_audio_paths[lane_id-1])[0] + '.mp3'
    audio_path_wav = os.path.splitext(lane_audio_paths[lane_id-1])[0] + '.wav'
    # Save video
    if video and video.filename.endswith('.mp4'):
        video.save(video_path)
        # Reinitialize cap
        if lane_caps[lane_id-1]:
            lane_caps[lane_id-1].release()
        lane_caps[lane_id-1] = cv2.VideoCapture(video_path)
    # Save audio and run ambulance detection
    if audio and (audio.filename.endswith('.mp3') or audio.filename.endswith('.wav')):
        ext = os.path.splitext(audio.filename)[1].lower()
        audio_path = audio_path_mp3 if ext == '.mp3' else audio_path_wav
        audio.save(audio_path)
        # Run ambulance siren detection
        detected = detect_ambulance_siren(audio_path)
        lane_ambulance_present[lane_id-1] = detected
        if detected:
            lane_ambulance_last_time[lane_id-1] = time.time()
            lane_eta_multiplier[lane_id-1] = 1/3  # Reduce ETA when ambulance detected
    return redirect(url_for('index'))

def init_lanes():
    for i in range(LANE_COUNT):
        video_path = lane_video_paths[i]
        if os.path.exists(video_path):
            cap = cv2.VideoCapture(video_path)
            lane_caps[i] = cap
        else:
            lane_caps[i] = None
init_lanes()

def generate_frames(lane_idx):
    global lane_caps
    cap = lane_caps[lane_idx]
    system = lane_systems[lane_idx]
    frame_count = 0
    start_time = time.time()
    while True:
        try:
            if cap is None or not cap.isOpened():
                # Try to open again if file exists
                video_path = lane_video_paths[lane_idx]
                if os.path.exists(video_path):
                    cap = cv2.VideoCapture(video_path)
                    lane_caps[lane_idx] = cap
                else:
                    placeholder = np.zeros((320, 480, 3), dtype=np.uint8)
                    cv2.putText(placeholder, f"Lane {lane_idx+1} No video uploaded", (30, 160), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
                    ret, buffer = cv2.imencode('.jpg', placeholder)
                    frame_bytes = buffer.tobytes()
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                    time.sleep(2)
                    continue
            success, frame = cap.read()
            if not success or frame is None:
                # Loop the video
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            # Only run YOLO every 3rd frame for speed, but always overlay text
            if frame_count % 3 == 0:
                processed_frame = system.process_frame(frame, run_detection=True)
            else:
                processed_frame = system.process_frame(frame, run_detection=False)
            # Overlay ambulance presence and adjust ETA if needed
            ambulance_present = lane_ambulance_present[lane_idx]
            if ambulance_present:
                # Reduce ETA if ambulance detected (e.g., divide by 3, min 5 sec)
                if hasattr(system, 'last_eta') and system.last_eta is not None:
                    system.last_eta = max(5, int(system.last_eta / 3))
                cv2.putText(processed_frame, "Ambulance Detected!", (10, 200), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
            ret, buffer = cv2.imencode('.jpg', processed_frame)
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            frame_count += 1
            if frame_count % 30 == 0:
                elapsed = time.time() - start_time
                print(f"Lane {lane_idx+1} streaming FPS: {frame_count / elapsed:.2f}")
        except Exception as ex:
            print(f"Exception in generate_frames for lane {lane_idx+1}: {ex}")
            placeholder = np.zeros((320, 480, 3), dtype=np.uint8)
            cv2.putText(placeholder, f"Lane {lane_idx+1} error", (30, 160), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
            ret, buffer = cv2.imencode('.jpg', placeholder)
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(2)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed/<int:lane_id>')
def video_feed(lane_id):
    return Response(generate_frames(lane_id - 1),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(debug=True)