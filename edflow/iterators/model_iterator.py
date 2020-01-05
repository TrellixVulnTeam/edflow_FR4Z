import signal, sys
from tqdm import tqdm, trange

from edflow.custom_logging import get_logger
from edflow.util import walk


class ShutdownRequest(Exception):
    """Raised when we receive a SIGTERM signal to shut down. Allows hooks to
    perform final actions such as writing a last checkpoint."""

    pass


class PyHookedModelIterator(object):
    """Implements a similar interface as the :class:`HookedModelIterator` to
    train framework independent models."""

    def __init__(
        self,
        config,
        root,
        model,
        dataset,
        hook_freq=100,
        num_epochs=100,
        hooks=[],
        bar_position=0,
        nogpu=False,
        desc="",
    ):
        """Constructor.

        Parameters
        ----------
        model : object
	    Model class.
        num_epochs : int
	    Number of times to iterate over the data.
        hooks : list
	    List containing :class:`Hook` instances.
        hook_freq : int
	    Frequency at which hooks are evaluated.
        bar_position : int
	    Used by tqdm to place bars at the right
            position when using multiple Iterators in parallel.
        """
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)

        self.config = config
        self.root = root

        self.model = model
        self.dataset = dataset
        self.num_epochs = num_epochs

        self.hooks = hooks
        self.epoch_hooks = list()

        self.hook_freq = hook_freq

        self.bar_pos = bar_position * 2
        self.desc = desc

        self.logger = get_logger(type(self).__name__)

        self._global_step = 0
        self._batch_step = 0
        self._epoch_step = 0
        self._split = None

    def get_split(self, *args, **kwargs):
        """Get the current split that is processed."""
        return self._split

    def get_global_step(self, *args, **kwargs):
        """Get the global step. The global step corresponds to the number of
        steps the model was trained for. It is updated in each step during
        training but not during evaluation."""
        return self._global_step

    def set_global_step(self, step):
        """Set the global step. Should be done when restoring a model from a
        checkpoint."""
        self._global_step = step

    def get_batch_step(self, *args, **kwargs):
        """Batch index of current run."""
        return self._batch_step

    def get_epoch_step(self, *args, **kwargs):
        """Epoch index of current run."""
        return self._epoch_step

    def reset_global_step(self):
        self.set_global_step(0)

    def increment_global_step(self, *args, **kwargs):
        if not self.config.get("test_mode", False):
            self._global_step += 1
        return self._global_step

    def make_feeds(self, batch):
        # copy of batches
        feeds = walk(batch, lambda val: val)
        return feeds

    def _handle_sigterm(self, signum, frame):
        e = ShutdownRequest()
        self._handle_exception(e)
        sys.exit(0)

    def _handle_exception(self, e):
        for hook in self.hooks:
            hook.at_exception(e)

    def iterate(self, batch_iterator, batch_iterator_validation=None):
        """Iterates over the data supplied and feeds it to the model.

        Parameters
        ----------
        batch_iterator : Iterable
	    Iterable returning training data.
        batch_iterator_validation : Iterable
	    Iterable returning validation data or None
        """

        try:
            self._iterate(batch_iterator, batch_iterator_validation)
        except Exception as e:
            self._handle_exception(e)
            raise e

    def _iterate(self, batch_iterator, batch_iterator_validation=None):
        """Iterates over the data supplied and feeds it to the model.

        Parameters
        ----------
        batch_iterator : Iterable
	    Iterable returning training data.
        """

        step_ops = self.step_ops()

        pos = self.bar_pos
        base = self.desc + " - " if self.desc != "" else ""
        desc_epoch = base + "Epoch"
        desc_batch = base + "Batch"

        validation_frequency = self.config.get(
            "val_freq", self.config.get("log_freq", -1)
        )
        batches = {"train": batch_iterator, "validation":
                   batch_iterator_validation}
        if self.config.get("test_mode", False) and batches["validation"] is None:
            self.logger.warning("No validation batch specified, defaulting to train batch.")
            batches["validation"] = batches["train"]
        batches = dict((s, b) for s, b in batches.items() if b is not None)

        num_epochs = 1 if self.config.get("test_mode", False) else self.num_epochs
        for epoch_step in trange(num_epochs, desc=desc_epoch,
                                 position=pos, dynamic_ncols=True):
            self._epoch_step = epoch_step

            ############# run one batch on each split until new epoch or max steps
            self.run_hooks(epoch_step, before=True)

            num_steps = 0 if self.config.get("test_mode", False) else len(batches["train"])
            for batch_step in trange(num_steps, desc=desc_batch,
                                     position = pos+1, dynamic_ncols=True):
                self._batch_step = batch_step

                def lazy_split_op(split):
                    def split_op():
                        self._split = split
                        batch = next(batches[split])
                        feeds = self.make_feeds(batch)
                        fetches = {"step_ops": step_ops}
                        self.run_hooks(batch_step, fetches, feeds, batch, before=True)
                        return self.run(fetches, feed_dict=feeds)
                    return split_op
                results = {"global_step": self.get_global_step()}
                for split in batches:
                    results[split] = lazy_split_op(split)
                self.run_hooks(batch_step, results=results, before=False)
                del results

                self.increment_global_step()

                if batches["train"].is_new_epoch or self.get_global_step() >= self.config.get(
                    "num_steps", float("inf")
                ):
                    self.logger.info("Done with epoch")
                    batches["train"].reset()
                    break
            self.run_hooks(epoch_step, before=False)

            ############# run one epoch on each split
            # only continue a split as long as someone is retrieving results
            for split in batches:
                self.run_hooks(epoch_step, before=True, epoch_hooks=True)

                for batch_step in trange(len(batches[split]), desc=split,
                                         position = pos+2, dynamic_ncols=True):
                    self._batch_step = batch_step

                    active = False
                    def lazy_split_op(split):
                        def split_op():
                            nonlocal active
                            active = True
                            self._split = split
                            batch = next(batches[split])
                            feeds = self.make_feeds(batch)
                            fetches = {"step_ops": step_ops}
                            self.run_hooks(batch_step, fetches, feeds, batch,
                                           before=True, epoch_hooks=True)
                            return self.run(fetches, feed_dict=feeds)
                        return split_op
                    results = {"global_step": self.get_global_step(),
                               split: lazy_split_op(split)}
                    self.run_hooks(batch_step, results=results, before=False,
                                   epoch_hooks=True)
                    del results

                    if batches[split].is_new_epoch or not active:
                        self.logger.info("Done with {}".format(split))
                        batches[split].reset()
                        break
                self.run_hooks(epoch_step, before=False, epoch_hooks=True)

    def run(self, fetches, feed_dict):
        """Runs all fetch ops and stores the results.

        Parameters
        ----------
        fetches : dict
	    name: Callable pairs.
        feed_dict : dict
	    Passed as kwargs to all fetch ops

        Returns
        -------
        dict
            name: results pairs.
        """

        def fn(fetch_fn):
            return fetch_fn(self.model, **feed_dict)

        results = walk(fetches, fn)

        return results

    def run_hooks(
        self, index, fetches=None, feeds=None, batch=None, results=None,
        before=True, epoch_hooks=False
    ):
        """Run all hooks and manage their stuff. The passed arguments determine
        which method of the hooks is called.

        Parameters
        ----------
        index : int
	    Current epoch or batch index. This is not necessarily
            the global training step.
        fetches : list or dict
	    Fetches for the next session.run call.
        feeds : dict
	    Feeds for the next session.run call.
        results : same as fetches
	    Results from the last session.run call.
        before : bool
	    If not obvious determines if the before or after
            methods of the hooks should be called.

        Returns
        -------
        test : same as fetches
	    Updated fetches.
        test : dict
	    Updated feeds
        """

        is_step = fetches is not None and feeds is not None
        is_step = is_step or results is not None

        condition = self._global_step % self.hook_freq == 0 or not is_step

        hooks = self.hooks if not epoch_hooks else self.epoch_hooks
        if condition:
            for hook in hooks:
                if before:
                    if is_step:
                        hook.before_step(index, fetches, feeds, batch)
                    else:
                        hook.before_epoch(index)
                else:
                    if is_step:
                        hook.after_step(index, results)
                    else:
                        hook.after_epoch(index)

    def step_ops(self):
        """Defines ops that are called at each step.

        Returns
        -------
            The operation run at each step."""

        raise NotImplementedError()

    def initialize(self, checkpoint_path=None):
        pass
