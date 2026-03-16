import os
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import cv2
import numpy as np
import pyttsx3
import tensorflow as tf
from tensorflow.keras.applications import MobileNetV2 #type:ignore
from tensorflow.keras.layers import Dense, GlobalAveragePooling2D #type:ignore
from tensorflow.keras.models import Model #type:ignore
from tensorflow.keras.applications.mobilenet_v2 import preprocess_input #type:ignore
from mtcnn import MTCNN

# Blink detection imports
import dlib
from scipy.spatial import distance
from imutils import face_utils


engine = pyttsx3.init()

def speak(text):
    engine.say(text)
    engine.runAndWait()


# Eye Aspect Ratio
def eye_aspect_ratio(eye):

    A = distance.euclidean(eye[1], eye[5])
    B = distance.euclidean(eye[2], eye[4])
    C = distance.euclidean(eye[0], eye[3])

    ear = (A + B) / (2.0 * C)
    return ear


emotion_labels = np.load("faceemotion_labels.npy", allow_pickle=True)
print("Emotion Labels:", emotion_labels)


base_model = MobileNetV2(
    weights=None,
    include_top=False,
    input_shape=(224,224,3)
)

x = base_model.output
x = GlobalAveragePooling2D()(x)
x = Dense(128, activation="relu")(x)
output = Dense(len(emotion_labels), activation="softmax")(x)

model = Model(inputs=base_model.input, outputs=output)

model.load_weights(
    "faceemotion_mtcnn_mobilenetv2.h5",
    by_name=True,
    skip_mismatch=True
)

print("Model Loaded Successfully")


detector = MTCNN()

# Blink detection setup
dlib_detector = dlib.get_frontal_face_detector()
predictor = dlib.shape_predictor("shape_predictor_68_face_landmarks.dat")

(lStart, lEnd) = face_utils.FACIAL_LANDMARKS_IDXS["left_eye"]
(rStart, rEnd) = face_utils.FACIAL_LANDMARKS_IDXS["right_eye"]

EAR_THRESHOLD = 0.25
EAR_FRAMES = 3

blink_count = 0
frame_counter = 0


mental_state_map = {
    "happy": "Positive",
    "surprise": "Excited",
    "sadness": "Low Mood",
    "anger": "Stressed",
    "fear": "Anxious",
    "disgust": "Uncomfortable",
    "contempt": "Negative"
}


recommendation_map = {
    "Positive": "Keep up the good mood",
    "Excited": "Take a moment to relax",
    "Low Mood": "Listen to music or talk with a friend",
    "Stressed": "Try deep breathing",
    "Anxious": "Practice meditation",
    "Uncomfortable": "Take a short walk",
    "Negative": "Focus on positive activities"
}


cap = cv2.VideoCapture(0)

last_emotion = ""

print("Press Q to exit")

while True:

    ret, frame = cap.read()
    if not ret:
        break


    img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


    # ---------- BLINK DETECTION ----------

    faces_dlib = dlib_detector(gray)

    for rect in faces_dlib:

        shape = predictor(gray, rect)
        shape = face_utils.shape_to_np(shape)

        leftEye = shape[lStart:lEnd]
        rightEye = shape[rStart:rEnd]

        leftEAR = eye_aspect_ratio(leftEye)
        rightEAR = eye_aspect_ratio(rightEye)

        ear = (leftEAR + rightEAR) / 2.0

        if ear < EAR_THRESHOLD:
            frame_counter += 1
        else:
            if frame_counter >= EAR_FRAMES:
                blink_count += 1
            frame_counter = 0

    cv2.putText(
        frame,
        "Blinks: " + str(blink_count),
        (10,40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255,0,0),
        2
    )


    # ---------- EMOTION DETECTION ----------

    faces = detector.detect_faces(img_rgb)

    for face in faces:

        x, y, w, h = face['box']

        x = max(0, x)
        y = max(0, y)

        face_img = img_rgb[y:y+h, x:x+w]

        try:

            face_img = cv2.resize(face_img, (224,224))
            face_img = preprocess_input(face_img)
            face_img = np.expand_dims(face_img, axis=0)

            preds = model.predict(face_img, verbose=0)

            emotion_index = np.argmax(preds)

            emotion = emotion_labels[emotion_index]

        except:
            emotion = "Unknown"


        mental_state = mental_state_map.get(emotion, "Unknown")
        recommendation = recommendation_map.get(mental_state, "")


        cv2.rectangle(frame,(x,y),(x+w,y+h),(0,255,0),2)


        cv2.putText(
            frame,
            "Emotion: " + emotion,
            (x,y-40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0,255,0),
            2
        )


        cv2.putText(
            frame,
            "State: " + mental_state,
            (x,y-20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255,255,0),
            2
        )


        cv2.putText(
            frame,
            "Advice: " + recommendation,
            (x,y+h+25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0,255,255),
            2
        )


        if emotion != last_emotion and emotion != "Unknown":

            speak("Emotion detected " + emotion)
            speak("Recommendation " + recommendation)

            last_emotion = emotion


    cv2.imshow("Emotion detection and blink detection", frame)


    if cv2.waitKey(1) & 0xFF == ord('q'):
        break


cap.release()
cv2.destroyAllWindows()