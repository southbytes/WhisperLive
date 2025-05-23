import json
import logging
import threading
import time

from openvino import Core
from whisper_live.backend.base import ServeClientBase
from whisper_live.transcriber.transcriber_openvino import WhisperOpenVINO


class ServeClientOpenVINO(ServeClientBase):
    SINGLE_MODEL = None
    SINGLE_MODEL_LOCK = threading.Lock()

    def __init__(self, websocket, task="transcribe", device=None, language=None, client_uid=None, model="small.en",
                 initial_prompt=None, vad_parameters=None, use_vad=True, single_model=False):
        """
        Initialize a ServeClient instance.
        The Whisper model is initialized based on the client's language and device availability.
        The transcription thread is started upon initialization. A "SERVER_READY" message is sent
        to the client to indicate that the server is ready.

        Args:
            websocket (WebSocket): The WebSocket connection for the client.
            task (str, optional): The task type, e.g., "transcribe." Defaults to "transcribe".
            device (str, optional): The device type for Whisper, "cuda" or "cpu". Defaults to None.
            language (str, optional): The language for transcription. Defaults to None.
            client_uid (str, optional): A unique identifier for the client. Defaults to None.
            model (str, optional): Huggingface model_id for a valid OpenVINO model.
            initial_prompt (str, optional): Prompt for whisper inference. Defaults to None.
            single_model (bool, optional): Whether to instantiate a new model for each client connection. Defaults to False.
        """
        super().__init__(client_uid, websocket)
        self.language = "en" if language is None else language
        if not self.language.startswith("<|"):
            self.language = f"<|{self.language}|>"

        self.task = "transcribe" if task is None else task
        self.same_output_threshold = 10
        self.end_time_for_same_output = None
        self.clip_audio = True

        core = Core()
        available_devices = core.available_devices
        if 'GPU' in available_devices:
            selected_device = 'GPU'
        else:
            gpu_devices = [d for d in available_devices if d.startswith('GPU')]
            selected_device = gpu_devices[0] if gpu_devices else 'CPU'
        self.device = selected_device


        if single_model:
            if ServeClientOpenVINO.SINGLE_MODEL is None:
                self.create_model(model)
                ServeClientOpenVINO.SINGLE_MODEL = self.transcriber
            else:
                self.transcriber = ServeClientOpenVINO.SINGLE_MODEL
        else:
            self.create_model(model)

        # threading
        self.trans_thread = threading.Thread(target=self.speech_to_text)
        self.trans_thread.start()

        self.websocket.send(json.dumps({
            "uid": self.client_uid,
            "message": self.SERVER_READY,
            "backend": "openvino"
        }))
        logging.info(f"Using OpenVINO device: {self.device}")
        logging.info(f"Running OpenVINO backend with language: {self.language} and task: {self.task}")

    def create_model(self, model_id):
        """
        Instantiates a new model, sets it as the transcriber.
        """
        self.transcriber = WhisperOpenVINO(
            model_id,
            device=self.device,
            language=self.language,
            task=self.task
        )

    def transcribe_audio(self, input_sample):
        """
        Transcribes the provided audio sample using the configured transcriber instance.

        If the language has not been set, it updates the session's language based on the transcription
        information.

        Args:
            input_sample (np.array): The audio chunk to be transcribed. This should be a NumPy
                                    array representing the audio data.

        Returns:
            The transcription result from the transcriber. The exact format of this result
            depends on the implementation of the `transcriber.transcribe` method but typically
            includes the transcribed text.
        """
        if ServeClientOpenVINO.SINGLE_MODEL:
            ServeClientOpenVINO.SINGLE_MODEL_LOCK.acquire()
        result = self.transcriber.transcribe(input_sample)
        if ServeClientOpenVINO.SINGLE_MODEL:
            ServeClientOpenVINO.SINGLE_MODEL_LOCK.release()
        return result

    def handle_transcription_output(self, result, duration):
        """
        Handle the transcription output, updating the transcript and sending data to the client.

        Args:
            result (str): The result from whisper inference i.e. the list of segments.
            duration (float): Duration of the transcribed audio chunk.
        """
        segments = []
        if len(result):
            self.t_start = None
            last_segment = self.update_segments(result, duration)
            segments = self.prepare_segments(last_segment)
        else:
            # show previous output if there is pause i.e. no output from whisper
            segments = self.get_previous_output()

        if len(segments):
            self.send_transcription_to_client(segments)
