import os
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"
import cv2
import numpy as np
from mtcnn import MTCNN

from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score

from tensorflow.keras.utils import to_categorical #type:ignore
from tensorflow.keras.applications import MobileNetV2 #type:ignore
from tensorflow.keras.applications.mobilenet_v2 import preprocess_input #type:ignore
from tensorflow.keras.layers import Dense, GlobalAveragePooling2D    #type:ignore
from tensorflow.keras.models import Model #type:ignore
from tensorflow.keras.optimizers import Adam #type:ignore
from tensorflow.keras.preprocessing.image import ImageDataGenerator #type:ignore

DATASET_PATH = "C:\\Users\\acer\\Downloads\\ck+"

detector = MTCNN()

X = []
y = []

emotion_labels = sorted(os.listdir(DATASET_PATH))
print("Training Label Order:", emotion_labels)

for label in emotion_labels:

    folder = os.path.join(DATASET_PATH, label)

    for img_name in os.listdir(folder):

        img_path = os.path.join(folder, img_name)

        img = cv2.imread(img_path)

        if img is None:
            continue

        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        faces = detector.detect_faces(img_rgb)

        if len(faces) > 0:

            x, y_, w, h = faces[0]['box']

            x = max(0, x)
            y_ = max(0, y_)

            face = img_rgb[y_:y_+h, x:x+w]

            try:
                face = cv2.resize(face, (224,224))
                X.append(face)
                y.append(emotion_labels.index(label))
            except:
                pass

X = np.array(X)
X = preprocess_input(X)

y = to_categorical(y)

print("Dataset shape:", X.shape)

X_train, X_val, y_train, y_val = train_test_split(
    X, y,
    test_size=0.2,
    random_state=42,
    stratify=y
)
base_model = MobileNetV2(
    weights='imagenet',
    include_top=False,
    input_shape=(224,224,3)
)

# Freeze layers
for layer in base_model.layers[:-30]:
    layer.trainable = False

for layer in base_model.layers[-30:]:
    layer.trainable = True

x = base_model.output
x = GlobalAveragePooling2D()(x)
x = Dense(128, activation='relu')(x)

output = Dense(len(emotion_labels), activation='softmax')(x)

model = Model(inputs=base_model.input, outputs=output)

model.compile(
    optimizer=Adam(learning_rate=0.0001),
    loss='categorical_crossentropy',
    metrics=['accuracy']
)

model.summary()
datagen = ImageDataGenerator(
    rotation_range=20,
    zoom_range=0.2,
    horizontal_flip=True
)

history = model.fit(
    datagen.flow(X_train, y_train, batch_size=32),
    validation_data=(X_val, y_val),
    epochs=25
)
loss, accuracy = model.evaluate(X_val, y_val)
print("Validation Accuracy:", accuracy)
y_pred_prob = model.predict(X_val)

y_pred = np.argmax(y_pred_prob, axis=1)
y_true = np.argmax(y_val, axis=1)

accuracy = accuracy_score(y_true, y_pred)
precision = precision_score(y_true, y_pred, average='weighted')
recall = recall_score(y_true, y_pred, average='weighted')
f1 = f1_score(y_true, y_pred, average='weighted')

print("Accuracy:", accuracy)
print("Precision:", precision)
print("Recall:", recall)
print("F1 Score:", f1)

model.save("faceemotion_mtcnn_mobilenetv2.h5")

np.save("faceemotion_labels.npy", emotion_labels)

print("Model and labels saved successfully!")