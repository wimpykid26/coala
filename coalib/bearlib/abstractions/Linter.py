from contextlib import contextmanager
from functools import partial
import inspect
from itertools import chain, compress
import shutil
from subprocess import check_call, CalledProcessError, DEVNULL

from coalib.bears.LocalBear import LocalBear
from coalib.misc.ContextManagers import make_temp
from coala_decorators.decorators import assert_right_type, enforce_signature
from coalib.misc.Shell import run_shell_command
from coalib.settings.FunctionMetadata import FunctionMetadata

from coalib.bearlib.abstractions.linterformats import (
    create_regex_format_class, create_corrected_format_class


_format_classes = (create_regex_format_class, create_corrected_format_class)


def _prepare_options(options):
    """
    Prepares options for ``linter`` for a given options dict in-place.

    :param options:
        The options dict that contains user/developer inputs.
    :return:
        The format-class or ``None`` if no output-format was specified.
    """
    allowed_options = {"executable",
                       "output_format",
                       "use_stdin",
                       "use_stdout",
                       "use_stderr",
                       "config_suffix",
                       "executable_check_fail_info",
                       "prerequisite_check_command"}

    if not options["use_stdout"] and not options["use_stderr"]:
        raise ValueError("No output streams provided at all.")

    if options["prerequisite_check_command"]:
        if "prerequisite_check_fail_message" in options:
            assert_right_type(options["prerequisite_check_fail_message"],
                              str,
                              "prerequisite_check_fail_message")
        else:
            options["prerequisite_check_fail_message"] = (
                "Prerequisite check failed.")

        allowed_options.add("prerequisite_check_fail_message")

    # The format class iterator is expected to yield three things:
    # - The name of the format as a string.
    # - The set of option names that got processed.
    # - The actual format-class that handles the result processing.
    format_class_setup_iterators = {
        next(it): it for it in (fmt() for fmt in _format_classes)}

    if options["output_format"] is not None:
        try:
            format_class = format_class_setup_iterators[
                options["output_format"]]
        except KeyError:
            raise ValueError("Invalid `output_format` specified.")

        # Get the option names that were processed.
        allowed_options |= next(format_class)

    # Check for illegal superfluous options.
    superfluous_options = options.keys() - allowed_options
    if superfluous_options:
        raise ValueError(
            "Invalid keyword arguments provided: " +
            ", ".join(repr(s) for s in sorted(superfluous_options)))

    return None if options["output_format"] is None else next(format_class)


def _create_linter(klass, options, format_class):
    class LinterMeta(type):

        def __repr__(cls):
            return "<{} linter class (wrapping {!r})>".format(
                cls.__name__, options["executable"])

    class LinterBase(LocalBear, metaclass=LinterMeta):

        @staticmethod
        def generate_config(filename, file):
            """
            Generates the content of a config-file the linter-tool might need.

            The contents generated from this function are written to a
            temporary file and the path is provided inside
            ``create_arguments()``.

            By default no configuration is generated.

            You can provide additional keyword arguments and defaults. These
            will be interpreted as required settings that need to be provided
            through a coafile-section.

            :param filename:
                The name of the file currently processed.
            :param file:
                The contents of the file currently processed.
            :return:
                The config-file-contents as a string or ``None``.
            """
            return None

        @staticmethod
        def create_arguments(filename, file, config_file):
            """
            Creates the arguments for the linter.

            You can provide additional keyword arguments and defaults. These
            will be interpreted as required settings that need to be provided
            through a coafile-section.

            :param filename:
                The name of the file the linter-tool shall process.
            :param file:
                The contents of the file.
            :param config_file:
                The path of the config-file if used. ``None`` if unused.
            :return:
                A sequence of arguments to feed the linter-tool with.
            """
            raise NotImplementedError

        @staticmethod
        def get_executable():
            """
            Returns the executable of this class.

            :return:
                The executable name.
            """
            return options["executable"]

        @classmethod
        def check_prerequisites(cls):
            """
            Checks whether the linter-tool the bear uses is operational.

            :return:
                True if operational, otherwise a string containing more info.
            """
            if shutil.which(cls.get_executable()) is None:
                return (repr(cls.get_executable()) + " is not installed." +
                        (" " + options["executable_check_fail_info"]
                         if options["executable_check_fail_info"] else
                         ""))
            else:
                if options["prerequisite_check_command"]:
                    try:
                        check_call(options["prerequisite_check_command"],
                                   stdout=DEVNULL,
                                   stderr=DEVNULL)
                        return True
                    except (OSError, CalledProcessError):
                        return options["prerequisite_check_fail_message"]
                return True

        @classmethod
        def _get_create_arguments_metadata(cls):
            return FunctionMetadata.from_function(
                cls.create_arguments,
                omit={"self", "filename", "file", "config_file"})

        @classmethod
        def _get_generate_config_metadata(cls):
            return FunctionMetadata.from_function(
                cls.generate_config,
                omit={"filename", "file"})

        @classmethod
        def _get_process_output_metadata(cls):
            metadata = FunctionMetadata.from_function(cls.process_output)

            if options["output_format"] is None:
                omitted = {"self", "output", "filename", "file"}
            else:
                # If a specific output format is provided, function signatures
                # from process_output functions should not appear in the help.
                omitted = set(chain(metadata.non_optional_params,
                                    metadata.optional_params))

            metadata.omit = omitted
            return metadata

        @classmethod
        def get_metadata(cls):
            merged_metadata = FunctionMetadata.merge(
                cls._get_process_output_metadata(),
                cls._get_generate_config_metadata(),
                cls._get_create_arguments_metadata())
            merged_metadata.desc = (
                "{}\n\nThis bear uses the {!r} tool.".format(
                    inspect.getdoc(cls), cls.get_executable()))

            return merged_metadata

        if options["output_format"] is None:
            # Check if user supplied a `process_output` override.
            if not callable(getattr(klass, "process_output", None)):
                raise ValueError("`process_output` not provided by given "
                                 "class {!r}.".format(klass.__name__))
                # No need to assign to `process_output` here, the class mixing
                # below automatically does that.
        else:
            # Prevent people from accidentally defining `process_output`
            # manually, as this would implicitly override the internally
            # set-up `process_output` from the format-class mixin.
            if hasattr(klass, "process_output"):
                raise ValueError("Found `process_output` already defined "
                                 "by class {!r}, but {!r} output-format is "
                                 "specified.".format(klass.__name__,
                                                     options["output_format"]))

        @classmethod
        @contextmanager
        def _create_config(cls, filename, file, **kwargs):
            """
            Provides a context-manager that creates the config file if the
            user provides one and cleans it up when done with linting.

            :param filename:
                The filename of the file.
            :param file:
                The file contents.
            :param kwargs:
                Section settings passed from ``run()``.
            :return:
                A context-manager handling the config-file.
            """
            content = cls.generate_config(filename, file, **kwargs)
            if content is None:
                yield None
            else:
                with make_temp(
                        suffix=options["config_suffix"]) as config_file:
                    with open(config_file, mode="w") as fl:
                        fl.write(content)
                    yield config_file

        def run(self, filename, file, **kwargs):
            # Get the **kwargs params to forward to `generate_config()`
            # (from `_create_config()`).
            generate_config_kwargs = FunctionMetadata.filter_parameters(
                self._get_generate_config_metadata(), kwargs)

            with self._create_config(
                    filename,
                    file,
                    **generate_config_kwargs) as config_file:
                # And now retrieve the **kwargs for `create_arguments()`.
                create_arguments_kwargs = (
                    FunctionMetadata.filter_parameters(
                        self._get_create_arguments_metadata(), kwargs))

                args = self.create_arguments(filename, file, config_file,
                                             **create_arguments_kwargs)

                try:
                    args = tuple(args)
                except TypeError:
                    self.err("The given arguments "
                             "{!r} are not iterable.".format(args))
                    return

                arguments = (self.get_executable(),) + args
                self.debug("Running '{}'".format(' '.join(arguments)))

                output = run_shell_command(
                    arguments,
                    stdin="".join(file) if options["use_stdin"] else None)

                output = tuple(compress(
                    output, (options["use_stdout"], options["use_stderr"])))
                if len(output) == 1:
                    output = output[0]

                process_output_kwargs = FunctionMetadata.filter_parameters(
                    self._get_process_output_metadata(), kwargs)
                return self.process_output(output, filename, file,
                                           **process_output_kwargs)

        def __repr__(self):
            return "<{} linter object (wrapping {!r}) at {}>".format(
                type(self).__name__, self.get_executable(), hex(id(self)))

    # Mixin the linter into the user-defined interface, otherwise
    # `create_arguments` and other methods would be overridden by the
    # default version.
    if options["output_format"] is None:
        inheritance_hierarchy = (klass, LinterBase)
    else:
        inheritance_hierarchy = (klass, format_class, LinterBase)

    result_klass = type(klass.__name__, inheritance_hierarchy, {})
    result_klass.__doc__ = klass.__doc__ if klass.__doc__ else ""
    return result_klass


@enforce_signature
def linter(executable: str,
           use_stdin: bool=False,
           use_stdout: bool=True,
           use_stderr: bool=False,
           config_suffix: str="",
           executable_check_fail_info: str="",
           prerequisite_check_command: tuple=(),
           output_format: (str, None)=None,
           **options):
    """
    Decorator that creates a ``LocalBear`` that is able to process results from
    an external linter tool.

    The main functionality is achieved through the ``create_arguments()``
    function that constructs the command-line-arguments that get parsed to your
    executable.

    >>> @linter("xlint", output_format="regex", output_regex="...")
    ... class XLintBear:
    ...     @staticmethod
    ...     def create_arguments(filename, file, config_file):
    ...         return "--lint", filename

    Requiring settings is possible like in ``Bear.run()`` with supplying
    additional keyword arguments (and if needed with defaults).

    >>> @linter("xlint", output_format="regex", output_regex="...")
    ... class XLintBear:
    ...     @staticmethod
    ...     def create_arguments(filename,
    ...                          file,
    ...                          config_file,
    ...                          lintmode: str,
    ...                          enable_aggressive_lints: bool=False):
    ...         arguments = ("--lint", filename, "--mode=" + lintmode)
    ...         if enable_aggressive_lints:
    ...             arguments += ("--aggressive",)
    ...         return arguments

    Sometimes your tool requires an actual file that contains configuration.
    ``linter`` allows you to just define the contents the configuration shall
    contain via ``generate_config()`` and handles everything else for you.

    >>> @linter("xlint", output_format="regex", output_regex="...")
    ... class XLintBear:
    ...     @staticmethod
    ...     def generate_config(filename,
    ...                         file,
    ...                         lintmode,
    ...                         enable_aggressive_lints):
    ...         modestring = ("aggressive"
    ...                       if enable_aggressive_lints else
    ...                       "non-aggressive")
    ...         contents = ("<xlint>",
    ...                     "    <mode>" + lintmode + "</mode>",
    ...                     "    <aggressive>" + modestring + "</aggressive>",
    ...                     "</xlint>")
    ...         return "\\n".join(contents)
    ...
    ...     @staticmethod
    ...     def create_arguments(filename,
    ...                          file,
    ...                          config_file):
    ...         return "--lint", filename, "--config", config_file

    As you can see you don't need to copy additional keyword-arguments you
    introduced from ``create_arguments()`` to ``generate_config()`` and
    vice-versa. ``linter`` takes care of forwarding the right arguments to the
    right place, so you are able to avoid signature duplication.

    If you override ``process_output``, you have the same feature like above
    (auto-forwarding of the right arguments defined in your function
    signature).

    Note when overriding ``process_output``: Providing a single output stream
    (via ``use_stdout`` or ``use_stderr``) puts the according string attained
    from the stream into parameter ``output``, providing both output streams
    inputs a tuple with ``(stdout, stderr)``. Providing ``use_stdout=False``
    and ``use_stderr=False`` raises a ``ValueError``. By default ``use_stdout``
    is ``True`` and ``use_stderr`` is ``False``.

    Documentation:
    Bear description shall be provided at class level.
    If you document your additional parameters inside ``create_arguments``,
    ``generate_config`` and ``process_output``, beware that conflicting
    documentation between them may be overridden. Document duplicated
    parameters inside ``create_arguments`` first, then in ``generate_config``
    and after that inside ``process_output``.

    For the tutorial see:
    http://coala.readthedocs.org/en/latest/Users/Tutorials/Linter_Bears.html

    :param executable:
        The linter tool.
    :param use_stdin:
        Whether the input file is sent via stdin instead of passing it over the
        command-line-interface.
    :param use_stdout:
        Whether to use the stdout output stream.
    :param use_stderr:
        Whether to use the stderr output stream.
    :param config_suffix:
        The suffix-string to append to the filename of the configuration file
        created when ``generate_config`` is supplied. Useful if your executable
        expects getting a specific file-type with specific file-ending for the
        configuration file.
    :param executable_check_fail_info:
        Information that is provided together with the fail message from the
        normal executable check. By default no additional info is printed.
    :param prerequisite_check_command:
        A custom command to check for when ``check_prerequisites`` gets
        invoked (via ``subprocess.check_call()``). Must be an ``Iterable``.
    :param prerequisite_check_fail_message:
        A custom message that gets displayed when ``check_prerequisites``
        fails while invoking ``prerequisite_check_command``. Can only be
        provided together with ``prerequisite_check_command``.
    :param output_format:
        The output format of the underlying executable. Valid values are

        - ``None``: Define your own format by overriding ``process_output``.
          Overriding ``process_output`` is then mandatory, not specifying it
          raises a ``ValueError``.
        - ``'regex'``: Parse output using a regex. See parameter
          ``output_regex``.
        - ``'corrected'``: The output is the corrected of the given file. Diffs
          are then generated to supply patches for results.

        Passing something else raises a ``ValueError``.
    :param output_regex:
        The regex expression as a string that is used to parse the output
        generated by the underlying executable. It should use as many of the
        following named groups (via ``(?P<name>...)``) to provide a good
        result:

        - line - The line where the issue starts.
        - column - The column where the issue starts.
        - end_line - The line where the issue ends.
        - end_column - The column where the issue ends.
        - severity - The severity of the issue.
        - message - The message of the result.
        - origin - The origin of the issue.
        - additional_info - Additional info provided by the issue.

        The groups ``line``, ``column``, ``end_line`` and ``end_column`` don't
        have to match numbers only, they can also match nothing, the generated
        ``Result`` is filled automatically with ``None`` then for the
        appropriate properties.

        Needs to be provided if ``output_format`` is ``'regex'``.
    :param severity_map:
        A dict used to map a severity string (captured from the
        ``output_regex`` with the named group ``severity``) to an actual
        ``coalib.results.RESULT_SEVERITY`` for a result. Severity strings are
        mapped **case-insensitive**!

        - ``RESULT_SEVERITY.MAJOR``: Mapped by ``error``.
        - ``RESULT_SEVERITY.NORMAL``: Mapped by ``warning`` or ``warn``.
        - ``RESULT_SEVERITY.MINOR``: Mapped by ``info``.

        A ``ValueError`` is raised when the named group ``severity`` is not
        used inside ``output_regex`` and this parameter is given.
    :param diff_severity:
        The severity to use for all results if ``output_format`` is
        ``'corrected'``. By default this value is
        ``coalib.results.RESULT_SEVERITY.NORMAL``. The given value needs to be
        defined inside ``coalib.results.RESULT_SEVERITY``.
    :param result_message:
        The message-string to use for all results. Can be used only together
        with ``corrected`` or ``regex`` output format. When using
        ``corrected``, the default value is ``"Inconsistency found."``, while
        for ``regex`` this static message is disabled and the message matched
        by ``output_regex`` is used instead.
    :param diff_distance:
        Number of unchanged lines that are allowed in between two changed lines
        so they get yielded as one diff if ``corrected`` output-format is
        given. If a negative distance is given, every change will be yielded as
        an own diff, even if they are right beneath each other. By default this
        value is ``1``.
    :raises ValueError:
        Raised when invalid options are supplied.
    :raises TypeError:
        Raised when incompatible types are supplied.
        See parameter documentations for allowed types.
    :return:
        A ``LocalBear`` derivation that lints code using an external tool.
    """
    options["executable"] = executable
    options["output_format"] = output_format
    options["use_stdin"] = use_stdin
    options["use_stdout"] = use_stdout
    options["use_stderr"] = use_stderr
    options["config_suffix"] = config_suffix
    options["executable_check_fail_info"] = executable_check_fail_info
    options["prerequisite_check_command"] = prerequisite_check_command

    return partial(_create_linter,
                   options=options,
                   format_class=_prepare_options(options))
