# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014-2016 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""Module containing command managers (SearchRunner and CommandRunner)."""

import collections
import traceback

from PyQt5.QtCore import pyqtSlot, QUrl, QObject

from qutebrowser.config import config, configexc
from qutebrowser.commands import cmdexc, cmdutils
from qutebrowser.utils import message, log, objreg, qtutils
from qutebrowser.misc import split


ParseResult = collections.namedtuple('ParseResult', ['cmd', 'args', 'cmdline',
                                                     'count'])
last_command = {}


def _current_url(tabbed_browser):
    """Convenience method to get the current url."""
    try:
        return tabbed_browser.current_url()
    except qtutils.QtValueError as e:
        msg = "Current URL is invalid"
        if e.reason:
            msg += " ({})".format(e.reason)
        msg += "!"
        raise cmdexc.CommandError(msg)


def replace_variables(win_id, arglist):
    """Utility function to replace variables like {url} in a list of args."""
    args = []
    tabbed_browser = objreg.get('tabbed-browser', scope='window',
                                window=win_id)
    if '{url}' in arglist:
        url = _current_url(tabbed_browser).toString(QUrl.FullyEncoded |
                                                    QUrl.RemovePassword)
    if '{url:pretty}' in arglist:
        pretty_url = _current_url(tabbed_browser).toString(QUrl.RemovePassword)
    for arg in arglist:
        if arg == '{url}':
            args.append(url)
        elif arg == '{url:pretty}':
            args.append(pretty_url)
        else:
            args.append(arg)
    return args


class CommandRunner(QObject):

    """Parse and run qutebrowser commandline commands.

    Attributes:
        _win_id: The window this CommandRunner is associated with.
        _partial_match: Whether to allow partial command matches.
    """

    def __init__(self, win_id, partial_match=False, parent=None):
        super().__init__(parent)
        self._partial_match = partial_match
        self._win_id = win_id

    def _get_alias(self, text):
        """Get an alias from the config.

        Args:
            text: The text to parse.

        Return:
            None if no alias was found.
            The new command string if an alias was found.
        """
        parts = text.strip().split(maxsplit=1)
        try:
            alias = config.get('aliases', parts[0])
        except (configexc.NoOptionError, configexc.NoSectionError):
            return None
        try:
            new_cmd = '{} {}'.format(alias, parts[1])
        except IndexError:
            new_cmd = alias
        if text.endswith(' '):
            new_cmd += ' '
        return new_cmd

    def parse_all(self, text, *args, **kwargs):
        """Split a command on ;; and parse all parts.

        If the first command in the commandline is a non-split one, it only
        returns that.

        Args:
            text: Text to parse.
            *args/**kwargs: Passed to parse().

        Yields:
            ParseResult tuples.
        """
        if ';;' in text:
            # Get the first command and check if it doesn't want to have ;;
            # split.
            first = text.split(';;')[0]
            result = self.parse(first, *args, **kwargs)
            if result.cmd.no_cmd_split:
                sub_texts = [text]
            else:
                sub_texts = [e.strip() for e in text.split(';;')]
        else:
            sub_texts = [text]
        for sub in sub_texts:
            yield self.parse(sub, *args, **kwargs)

    def _parse_count(self, cmdstr):
        """Split a count prefix off from a command for parse().

        Args:
            cmdstr: The command/args including the count.

        Return:
            A (count, cmdstr) tuple, with count being None or int.
        """
        if ':' not in cmdstr:
            return (None, cmdstr)

        count, cmdstr = cmdstr.split(':', maxsplit=1)
        try:
            count = int(count)
        except ValueError:
            # We just ignore invalid prefixes
            count = None
        return (count, cmdstr)

    def _parse_fallback(self, text, count, keep):
        """Parse the given commandline without a valid command."""
        if keep:
            cmdstr, sep, argstr = text.partition(' ')
            cmdline = [cmdstr, sep] + argstr.split()
        else:
            cmdline = text.split()
        return ParseResult(cmd=None, args=None, cmdline=cmdline, count=count)

    def parse(self, text, *, aliases=True, fallback=False, keep=False):
        """Split the commandline text into command and arguments.

        Args:
            text: Text to parse.
            aliases: Whether to handle aliases.
            fallback: Whether to do a fallback splitting when the command was
                      unknown.
            keep: Whether to keep special chars and whitespace

        Return:
            A ParseResult tuple.
        """
        cmdstr, sep, argstr = text.partition(' ')
        count, cmdstr = self._parse_count(cmdstr)

        if not cmdstr and not fallback:
            raise cmdexc.NoSuchCommandError("No command given")

        if aliases:
            new_cmd = self._get_alias(text)
            if new_cmd is not None:
                log.commands.debug("Re-parsing with '{}'.".format(new_cmd))
                return self.parse(new_cmd, aliases=False, fallback=fallback,
                                  keep=keep)

        if self._partial_match:
            cmdstr = self._completion_match(cmdstr)

        try:
            cmd = cmdutils.cmd_dict[cmdstr]
        except KeyError:
            if not fallback:
                raise cmdexc.NoSuchCommandError(
                    '{}: no such command'.format(cmdstr))
            return self._parse_fallback(text, count, keep)

        args = self._split_args(cmd, argstr, keep)
        if keep and args:
            cmdline = [cmdstr, sep + args[0]] + args[1:]
        elif keep:
            cmdline = [cmdstr, sep]
        else:
            cmdline = [cmdstr] + args[:]

        return ParseResult(cmd=cmd, args=args, cmdline=cmdline, count=count)

    def _completion_match(self, cmdstr):
        """Replace cmdstr with a matching completion if there's only one match.

        Args:
            cmdstr: The string representing the entered command so far

        Return:
            cmdstr modified to the matching completion or unmodified
        """
        matches = []
        for valid_command in cmdutils.cmd_dict.keys():
            if valid_command.find(cmdstr) == 0:
                matches.append(valid_command)
        if len(matches) == 1:
            cmdstr = matches[0]
        return cmdstr

    def _split_args(self, cmd, argstr, keep):
        """Split the arguments from an arg string.

        Args:
            cmd: The command we're currently handling.
            argstr: An argument string.
            keep: Whether to keep special chars and whitespace

        Return:
            A list containing the split strings.
        """
        if not argstr:
            return []
        elif cmd.maxsplit is None:
            return split.split(argstr, keep=keep)
        else:
            # If split=False, we still want to split the flags, but not
            # everything after that.
            # We first split the arg string and check the index of the first
            # non-flag args, then we re-split again properly.
            # example:
            #
            # input: "--foo -v bar baz"
            # first split: ['--foo', '-v', 'bar', 'baz']
            #                0        1     2      3
            # second split: ['--foo', '-v', 'bar baz']
            # (maxsplit=2)
            split_args = split.simple_split(argstr, keep=keep)
            flag_arg_count = 0
            for i, arg in enumerate(split_args):
                arg = arg.strip()
                if arg.startswith('-'):
                    if arg in cmd.flags_with_args:
                        flag_arg_count += 1
                else:
                    maxsplit = i + cmd.maxsplit + flag_arg_count
                    return split.simple_split(argstr, keep=keep,
                                              maxsplit=maxsplit)

            # If there are only flags, we got it right on the first try
            # already.
            return split_args

    def run(self, text, count=None):
        """Parse a command from a line of text and run it.

        Args:
            text: The text to parse.
            count: The count to pass to the command.
        """
        for result in self.parse_all(text):
            args = replace_variables(self._win_id, result.args)
            if count is not None:
                if result.count is not None:
                    raise cmdexc.CommandMetaError("Got count via command and "
                                                  "prefix!")
                result.cmd.run(self._win_id, args, count=count)
            elif result.count is not None:
                result.cmd.run(self._win_id, args, count=result.count)
            else:
                result.cmd.run(self._win_id, args)

            mode_manager = objreg.get('mode-manager', scope='window',
                                      window=self._win_id)
            if result.cmdline[0] not in ['leave-mode', 'command-accept',
                                         'repeat-command']:
                last_command[mode_manager.mode] = (
                    self._parse_count(text)[1],
                    count if count is not None else result.count)

    @pyqtSlot(str, int)
    @pyqtSlot(str)
    def run_safely(self, text, count=None):
        """Run a command and display exceptions in the statusbar."""
        try:
            self.run(text, count)
        except (cmdexc.CommandMetaError, cmdexc.CommandError) as e:
            message.error(self._win_id, e, immediately=True,
                          stack=traceback.format_exc())

    @pyqtSlot(str, int)
    def run_safely_init(self, text, count=None):
        """Run a command and display exceptions in the statusbar.

        Contrary to run_safely, error messages are queued so this is more
        suitable to use while initializing.
        """
        try:
            self.run(text, count)
        except (cmdexc.CommandMetaError, cmdexc.CommandError) as e:
            message.error(self._win_id, e, stack=traceback.format_exc())
