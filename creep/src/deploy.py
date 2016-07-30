#!/usr/bin/env python

from action import Action
from definition import Definition
from environment import Environment
from revision import Revision

import factory
import os
import path
import shutil
import tempfile

def execute (logger, base_path, definition_path, environment_path, names, append_files, remove_files, rev_from, rev_to, yes):
	# Ensure base directory is valid
	if not os.path.isdir (base_path):
		logger.error ('Base directory "{0}" doesn\'t exist.'.format (base_path))

		return False

	# Load environment file or fail
	full_path = os.path.join (base_path, environment_path)

	if os.path.isfile (full_path):
		with open (full_path, 'rb') as file:
			environment = Environment (file)
	else:
		logger.error ('No environment file "{0}" found.'.format (full_path))

		return False

	# Load definition file or use default
	full_path = os.path.join (base_path, definition_path)

	if os.path.isfile (full_path):
		with open (full_path, 'rb') as file:
			definition = Definition (file, [environment_path, definition_path])
	else:
		definition = Definition (None, [environment_path, definition_path])

	# Expand location names
	if len (names) < 1:
		names.append ('default')
	elif len (names) == 1 and names[0] == '*':
		names = environment.locations.keys ()

	# Deploy to target locations
	ok = True

	for name in names:
		location = environment.get_location (name)

		if location is None:
			logger.error ('There is no location "{0}" in your environment file.'.format (name))

			continue

		if location.connection is not None and not process (logger, definition, location, base_path, append_files, remove_files, rev_from, rev_to, yes):
			ok = False

			continue

		for sub_path, sub_names in location.subsidiaries.iteritems ():
			full_path = os.path.join (base_path, sub_path)

			logger.info ('Entering subsidiary path "{0}"'.format (full_path))

			ok = execute (logger, full_path, definition_path, environment_path, sub_names, [], [], None, None, yes) and ok

			logger.info ('Leaving subsidiary path "{0}"'.format (full_path))

	return ok

def process (logger, definition, location, base_path, append_files, remove_files, rev_from, rev_to, yes):
	# Build target from location connection string
	target = factory.create_target (logger, location.connection, location.options)

	if target is None:
		logger.error ('Unsupported scheme in connection string "{1}" for location "{0}".'.format (location.name, location.connection))

		return False

	# Read revision file
	if not location.local:
		data = target.read (logger, location.state)
	elif os.path.exists (os.path.join (base_path, location.state)):
		data = open (os.path.join (base_path, location.state), 'rb').read ()
	else:
		data = ''

	if data is None:
		logger.error ('Can\'t read contents of revision file "{1}" from location "{0}".'.format (location.name, location.state))

		return False

	try:
		revision = Revision (data)
	except Error as e:
		logger.error ('Can\'t parse revision from file "{1}" from location "{0}": {2}.'.format (location.name, location.state, e))

		return False

	# Build source repository reader from current directory
	source = factory.create_source (definition.source, definition.options, base_path)

	if source is None:
		logger.error ('Unknown source type in folder "{0}", try specifying "source" option in definition file.'.format (base_path))

		return False

	# Retrieve source and target revision
	if rev_from is None:
		rev_from = revision.get (location.name)

		if rev_from is None and not yes and not prompt (logger, 'No current revision found for location "{0}", maybe you\'re deploying for the first time. Initiate full deploy? [Y/N]'.format (location.name)):
			return True

	if rev_to is None:
		rev_to = source.current (base_path)

		if rev_to is None:
			logger.error ('Can\'t find source version for location "{0}", please ensure your environment file is correctly defined.'.format (location.name))

			return False

	revision.set (location.name, rev_to)

	# Prepare actions
	work_path = tempfile.mkdtemp ()

	try:
		# Append actions from revision diff
		source_actions = source.diff (logger, base_path, work_path, rev_from, rev_to)

		if source_actions is None:
			return False

		# Append actions for manually specified files
		manual_actions = []

		for append in location.append_files + append_files:
			full_path = os.path.join (base_path, append)

			if os.path.isdir (full_path):
				for (dirpath, dirnames, filenames) in os.walk (full_path):
					parent_path = os.path.relpath (dirpath, base_path)

					manual_actions.extend ((Action (os.path.join (parent_path, filename), Action.ADD) for filename in filenames))
			elif os.path.isfile (full_path):
				manual_actions.append (Action (append, Action.ADD))
			else:
				logger.warning ('Can\'t append missing file "{0}".'.format (append))

		for action in manual_actions:
			if not path.duplicate (os.path.join (base_path, action.path), work_path, action.path):
				logger.warning ('Can\'t copy file "{0}".'.format (action.path))

		for remove in location.remove_files + remove_files:
			full_path = os.path.join (base_path, remove)

			if os.path.isdir (full_path):
				for (dirpath, dirnames, filenames) in os.walk (full_path):
					parent_path = os.path.relpath (dirpath, base_path)

					manual_actions.extend ((Action (os.path.join (parent_path, filename), Action.DEL) for filename in filenames))
			else:
				manual_actions.append (Action (remove, Action.DEL))

		# Apply pre-processing modifiers on actions
		actions = []
		cancels = []
		used = set ()

		for command in source_actions + manual_actions:
			(actions_append, cancels_append) = definition.apply (logger, work_path, command.path, command.type, used)

			actions.extend (actions_append)
			cancels.extend (cancels_append)

		for cancel in cancels:
			path.remove (work_path, cancel)

		# Update current revision (remote mode)
		if rev_from != rev_to and not location.local:
			with open (os.path.join (work_path, location.state), 'wb') as file:
				file.write (revision.serialize ())

			actions.append (Action (location.state, Action.ADD))

		# Display processed actions using console target
		if len (actions) < 1:
			logger.info ('No deployment required for location "{0}".'.format (location.name))

			return True

		from targets.console import ConsoleTarget

		console = ConsoleTarget ()
		console.send (logger, work_path, actions)

		if not yes and not prompt (logger, 'Execute synchronization? [Y/N]'):
			return True

		# Execute processed actions after ordering them by precedence
		actions.sort (key = lambda action: (action.order (), action.path))

		if not target.send (logger, work_path, actions):
			return False

		# Update current revision (local mode)
		if location.local:
			with open (os.path.join (base_path, location.state), 'wb') as file:
				file.write (revision.serialize ())

	finally:
		shutil.rmtree (work_path)

	logger.info ('Deployment to location "{0}" done.'.format (location.name))

	return True

def prompt (logger, question):
	logger.info (question)

	while True:
		answer = raw_input ()

		if answer == 'N' or answer == 'n':
			return False
		elif answer == 'Y' or answer == 'y':
			return True

		logger.warning ('Invalid answer')
