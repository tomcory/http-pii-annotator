from tqdm import tqdm


class Progress:
    """Reports the progress of the pipeline steps. This implementation reports nothing."""

    def start_step(self, name: str, total: int, done: int = 0):
        pass

    def advance(self):
        pass

    def finish_step(self):
        pass


class ConsoleProgress(Progress):
    """Displays a progress bar for each pipeline step."""

    def __init__(self):
        self._bar = None

    def start_step(self, name: str, total: int, done: int = 0):
        self._bar = tqdm(total=total, initial=done, desc=name, unit="msg", dynamic_ncols=True)

    def advance(self):
        if self._bar is not None:
            self._bar.update(1)

    def finish_step(self):
        if self._bar is not None:
            self._bar.close()
            self._bar = None
