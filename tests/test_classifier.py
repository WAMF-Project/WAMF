from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import speciesid


REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO_ROOT / 'model.tflite'


class FakeInterpreter:
    def __init__(self, *, model_path, num_threads):
        self.model_path = model_path
        self.num_threads = num_threads
        self.allocate_calls = 0
        self.invoke_calls = 0
        self.input = None
        self.output = np.zeros((1, 965), dtype=np.uint8)
        self.output[0, 665] = 22

    def allocate_tensors(self):
        self.allocate_calls += 1

    def get_input_details(self):
        return [{
            'index': 170,
            'shape': np.array([1, 224, 224, 3]),
            'dtype': np.uint8,
            'quantization': (0.0078125, 128),
        }]

    def get_output_details(self):
        return [{
            'index': 171,
            'shape': np.array([1, 965]),
            'dtype': np.uint8,
            'quantization': (0.00390625, 0),
        }]

    def set_tensor(self, index, value):
        assert index == 170
        self.input = value

    def invoke(self):
        self.invoke_calls += 1

    def get_tensor(self, index):
        assert index == 171
        return self.output


def test_litert_initialization_and_category_compatibility():
    with patch('speciesid.Interpreter', FakeInterpreter):
        interpreter = speciesid.initialize_classifier(MODEL_PATH)

    image = np.zeros((224, 224, 3), dtype=np.uint8)
    categories = speciesid.classify(image)

    assert interpreter.model_path == str(MODEL_PATH)
    assert interpreter.num_threads == 4
    assert interpreter.allocate_calls == 1
    assert interpreter.invoke_calls == 1
    assert interpreter.input.shape == (1, 224, 224, 3)
    assert interpreter.input.flags.c_contiguous
    assert categories == [speciesid.Category(
        index=665,
        score=0.0859375,
        display_name='Cyanistes caeruleus',
        category_name='/m/01kvvt',
    )]


def test_classifier_filters_sorts_and_limits_results():
    with patch('speciesid.Interpreter', FakeInterpreter):
        interpreter = speciesid.initialize_classifier(MODEL_PATH)

    interpreter.output.fill(0)
    interpreter.output[0, 10] = 12  # Below the 0.05 threshold.
    interpreter.output[0, 20] = 13
    interpreter.output[0, 30] = 14
    interpreter.output[0, 40] = 15
    interpreter.output[0, 50] = 16
    interpreter.output[0, 60] = 17
    interpreter.output[0, 70] = 18

    categories = speciesid.classify(
        np.zeros((224, 224, 3), dtype=np.uint8)
    )

    assert [category.index for category in categories] == [70, 60, 50, 40, 30]
    assert len(categories) == speciesid.CLASSIFIER_MAX_RESULTS
    assert all(
        category.score >= speciesid.CLASSIFIER_SCORE_THRESHOLD
        for category in categories
    )
    assert [category.score for category in categories] == sorted(
        (category.score for category in categories), reverse=True
    )
    assert 10 not in [category.index for category in categories]


def test_golden_image_matches_existing_classification_and_name_database():
    speciesid.initialize_classifier(MODEL_PATH)
    image = Image.open(
        REPO_ROOT / 'media/wamf/snapshots/1779981851.294031-5nvuxv.jpg'
    ).convert('RGB').resize((224, 224))

    categories = speciesid.classify(np.asarray(image, dtype=np.uint8))

    assert categories == [speciesid.Category(
        index=665,
        score=0.0859375,
        display_name='Cyanistes caeruleus',
        category_name='/m/01kvvt',
    )]
    with patch('app.queries.NAMEDBPATH', str(REPO_ROOT / 'birdnames.db')):
        assert speciesid.get_common_name(categories[0].display_name) == 'Eurasian Blue Tit'
