# coding: utf8
import copy
import os
import threading
import time
import json
import traceback
import zipfile
import datetime
import subprocess

import utils
import pycraft
from Logger import Logger
from pycraft import authentication
from pycraft.networking.connection import Connection
from pycraft.networking.packets import Packet as PycraftPacket, clientbound, serverbound
from SARC.packet import Packet as SARCPacket
from pycraft.networking.types import Vector


class Config():
	def __init__(self, fileName):
		with open(fileName) as f:
			js = json.load(f)
		self.online_mode = js['online_mode']
		self.username = js['username']
		self.password = js['password']
		self.address = js['address']
		self.port = js['port']
		self.minimal_packets = js['minimal_packets']
		self.daytime = js['daytime']
		self.weather = js['weather']
		self.with_player_only = js['with_player_only']
		self.remove_items = js['remove_items']
		self.remove_bats = js['remove_bats']
		self.upload_file = js['upload_file']
		self.auto_relogin = js['auto_relogin']
		self.debug_mode = js['debug_mode']

class Recorder():
	socket_id = None

	def __init__(self, configFileName):
		self.config = Config(configFileName)
		self.recording = False
		self.online = False
		self.file_thread = None
		self.file_urls = []
		self.logger = Logger(name='Recorder', file_name='PCRC.log', display_debug=self.config.debug_mode)
		self.printConfig()

		if not self.config.online_mode:
			self.logger.log("Login in offline mode")
			self.connection = Connection(self.config.address, self.config.port, username=self.config.username)
		else:
			auth_token = authentication.AuthenticationToken()
			auth_token.authenticate(self.config.username, self.config.password)
			self.logger.log("Logged in as %s" % auth_token.profile.name)
			self.config.username = auth_token.profile.name
			self.connection = Connection(self.config.address, self.config.port, auth_token=auth_token)

		self.connection.register_packet_listener(self.onPacketReceived, PycraftPacket)
		self.connection.register_packet_listener(self.onPacketSent, PycraftPacket, outgoing=True)
		self.connection.register_packet_listener(self.onGameJoin, clientbound.play.JoinGamePacket)
		self.connection.register_packet_listener(self.onDisconnect, clientbound.play.DisconnectPacket)
		self.connection.register_packet_listener(self.onChatMessage, clientbound.play.ChatMessagePacket)

		self.protocolMap = {}
		self.logger.log('init finish')

	def __del__(self):
		self.stop()

	def printConfig(self):
		message = '------- Config --------\n'
		message += f'Online mode = {self.config.online_mode}\n'
		message += f'User name = {self.config.username}\n'
		message += f'Password = ******\n'
		message += f'Server address = {self.config.address}\n'
		message += f'Server port = {self.config.port}\n'
		message += f'Minimal packets mode = {self.config.minimal_packets}\n'
		message += f'Daytime set to = {self.config.daytime}\n'
		message += f'Weather switch = {self.config.weather}\n'
		message += f'Record with player only = {self.config.with_player_only}\n'
		message += f'Remove items = {self.config.remove_items}\n'
		message += f'Remove bats = {self.config.remove_bats}\n'
		message += f'Upload file to transfer.sh = {self.config.upload_file}\n'
		message += f'Auto relogin = {self.config.auto_relogin}\n'
		message += f'Debug mode = {self.config.debug_mode}\n'
		message += '----------------------'
		for line in message.splitlines():
			self.logger.log(line)

	def isOnline(self):
		return self.online

	def isRecording(self):
		return self.recording

	def onPacketSent(self, packet):
		self.logger.debug('<- {}'.format(packet.data))

	def onPacketReceived(self, packet):
		self.logger.debug('-> {}'.format(packet.data))
		self.processPacketData(packet)

	def onGameJoin(self, packet):
		self.logger.log('Connected to the server')
		self.online = True

	def onDisconnect(self, packet):
		self.logger.log('Disconnected from the server, reason = {}'.format(packet.json_data))
		self.online = False
		if self.isRecording() and self.config.auto_relogin:
			self.stop()
			for i in range(3):
				self.logger.log('Restart in {}s'.format(3 - i))
				time.sleep(1)
			self.start()

	def onChatMessage(self, packet):
		try:
			js = json.loads(packet.json_data)
			translate = js['translate']
			msg = js['with'][-1]
			message = '({}) '.format(packet.field_string('position'))
			try:
				name = js['with'][0]['insertion']
			except:
				name = None
			if translate == 'chat.type.announcement':  # from server
				message += '[Server] {}'.format(msg['text'])
				self.processCommand(msg['text'], None, None)
			elif translate == 'chat.type.text':  # chat
				message += '<{}> {}'.format(name, msg)
				uuid = js['with'][0]['hoverEvent']['value']['text'].split('"')[7]
				self.processCommand(msg, name, uuid)
			elif translate == 'commands.message.display.incoming':  # tell
				message += '<{}>(tell) {}'.format(name, msg['text'])
			elif translate in ['multiplayer.player.joined', 'multiplayer.player.left']:  # login in/out game
				message += '{} {} the game'.format(name, translate.split('.')[2])
			elif translate == 'chat.type.emote':  # me
				message += '* {} {}'.format(name, msg)
			else:
				message = packet.json_data
			print(message)
			self.logger.log(message, do_print=False)
		except:
			print(traceback.format_exc())
			pass

	def connect(self):
		if self.isOnline():
			self.logger.warn('Cannot connect when connected')
			return
		self.connection.connect()

	def disconnect(self):
		if not self.isOnline():
			self.logger.warn('Cannot disconnect when disconnected')
			return
		if len(self.file_urls) > 0:
			self.print_urls()
		self.chat('Bye')
		self.connection.disconnect()
		self.online = False

	def updatePlayerMovement(self, t=None):
		if t is None:
			t = utils.getMilliTime()
		self.last_player_movement = t

	def noPlayerMovement(self, t=None):
		if t is None:
			t = utils.getMilliTime()
		return t - self.last_player_movement >= 10 * 1000

	def timePassed(self, t=None):
		if t is None:
			t = utils.getMilliTime()
		return t - self.start_time

	def timeRecorded(self, t=None):
		if t is None:
			t = utils.getMilliTime()
		return self.timePassed(t) - self.afk_time

	def processPacketData(self, packet_raw):
		if not self.isRecording():
			return
		bytes = packet_raw.data
		if bytes[0] == 0x00:
			bytes = bytes[1:]
		t = utils.getMilliTime()
		packet_length = len(bytes)
		packet = SARCPacket()
		packet.receive(bytes)
		packet_recorded = copy.deepcopy(packet)
		packet_id = packet.read_varint()
		packet_name = self.protocolMap[str(packet_id)] if str(packet_id) in self.protocolMap else 'unknown'

		if packet_name == 'Player Position And Look (clientbound)':
			player_x = packet.read_double()
			player_y = packet.read_double()
			player_z = packet.read_double()
			self.pos = Vector(player_x, player_y, player_z)
			self.logger.log('Set self\'s position to {}'.format(utils.format_vector(self.pos)))

		if packet_recorded is not None and (packet_name in utils.BAD_PACKETS or (self.config.minimal_packets and packet_name in utils.USELESS_PACKETS)):
			packet_recorded = None

		if packet_recorded is not None and packet_name == 'Spawn Mob' and packet_length == 3:
			packet_recorded = None
			self.logger.log('nou wired packet')


		if packet_recorded is not None and 0 <= self.config.daytime < 24000 and packet_name == 'Time Update':
			self.logger.log('Set daytime to: ' + str(self.config.daytime))
			world_age = packet.read_long()
			packet_recorded = SARCPacket()
			packet_recorded.write_varint(packet_id)
			packet_recorded.write_long(world_age)
			packet_recorded.write_long(-self.config.daytime)  # If negative sun will stop moving at the Math.abs of the time
			utils.BAD_PACKETS.append('Time Update')  # Ignore all further updates

		# Remove weather if configured
		if packet_recorded is not None and not self.config.weather and packet_name == 'Change Game State':
			reason = packet.read_ubyte()
			if reason == 1 or reason == 2:
				packet_recorded = None

		if packet_recorded is not None and packet_name == 'Spawn Player':
			entity_id = packet.read_varint()
			uuid = packet.read_uuid()
			if entity_id not in self.player_ids:
				self.player_ids.append(entity_id)
			if uuid not in self.player_uuids:
				self.player_uuids.append(uuid)
				self.logger.log('Player added, uuid = {}'.format(uuid))
			self.updatePlayerMovement()

		# Keep track of spawned items and their ids
		if (packet_recorded is not None and
				(self.config.remove_items or self.config.remove_bats) and
				(packet_name == 'Spawn Object' or packet_name == 'Spawn Mob')):
			entity_id = packet.read_varint()
			entity_uuid = packet.read_uuid()
			entity_type = packet.read_byte()
			entity_name = None
			if self.config.remove_items and packet_name == 'Spawn Object' and entity_type == 34:
				entity_name = 'item'
			if self.config.remove_bats and packet_name == 'Spawn Mob' and entity_type == 3:
				entity_name = 'bat'
			if entity_name is not None:
				self.logger.debug('{} spawned but ignore and added to blocked id list'.format(entity_name))
				self.blocked_entity_ids.append(entity_id)
				packet_recorded = None

		# Removed destroyed blocked entity's id
		if packet_recorded is not None and packet_name == 'Destroy Entities':
			count = packet.read_varint()
			for i in range(count):
				entity_id = packet.read_varint()
				if entity_id in self.blocked_entity_ids:
					self.blocked_entity_ids.remove(entity_id)

		# Remove item pickup animation packet
		if packet_recorded is not None and self.config.remove_items and packet_name == 'Collect Item':
			collected_entity_id = packet.read_varint()
			if collected_entity_id in self.blocked_entity_ids:
				self.blocked_entity_ids.remove(collected_entity_id)
			packet_recorded = None

		# Detecting player activity to continue recording and remove items or bats
		if packet_name in utils.ENTITY_PACKETS:
			entity_id = packet.read_varint()
			if entity_id in self.player_ids:
				self.updatePlayerMovement()
			if entity_id in self.blocked_entity_ids:
				packet_recorded = None

		# Increase afk timer when recording stopped, afk timer prevents afk time in replays
		if self.config.with_player_only:
			noPlayerMovement = self.noPlayerMovement(t)
			if noPlayerMovement:
				self.afk_time += t - self.last_t
			if self.last_no_player_movement != noPlayerMovement:
				msg = 'Someone is nearby, continue recording now' if self.last_no_player_movement else 'Everyone left, pause recording now'
				self.chat(msg)
			self.last_no_player_movement = noPlayerMovement
		self.last_t = t


		# Recording
		if self.isRecording() and packet_recorded is not None and not (self.noPlayerMovement() and self.config.with_player_only):
			bytes = packet_recorded.read(packet_recorded.remaining())
			data = int(t - self.start_time).to_bytes(4, byteorder='big', signed=True)
			data += len(bytes).to_bytes(4, byteorder='big', signed=True)
			data += bytes
			self.write(data)
			self.logger.debug('{} packet recorded'.format(packet_name))

		if self.isRecording() and self.file_size > utils.FileSizeLimit:
			self.logger.log('tmcpr file size limit {}MB reached!'.format(utils.convert_file_size(utils.FileSizeLimit)))
			self.restart()

		if self.isRecording() and self.timeRecorded(t) > 1000 * 60 * 60 * 5:
			self.logger.log('5h recording reached!')
			self.restart()

		self.packet_counter += 1
		if int(self.timePassed(t) / (60 * 1000)) != self.last_showinfo_time or self.packet_counter - self.last_showinfo_packetcounter >= 100000:
			self.last_showinfo_time = int(self.timePassed(t) / (60 * 1000))
			self.last_showinfo_packetcounter = self.packet_counter
			self.logger.log('{} passed, {} packets recorded'.format(utils.convert_millis(self.timePassed(t)), self.packet_counter))

	def flush(self):
		if len(self.file_buffer) == 0:
			return
		with open(utils.RecordingFileName, 'ab+') as replay_recording:
			replay_recording.write(self.file_buffer)
		self.file_size += len(self.file_buffer)
		self.logger.log('Flushing {} bytes to "{}" file, file size = {}MB now'.format(
			len(self.file_buffer), utils.RecordingFileName, utils.convert_file_size(self.file_size)
		))
		self.file_buffer = bytearray()

	def write(self, data):
		self.file_buffer += data
		if len(self.file_buffer) > utils.FileBufferSize:
			self.flush()

	def createReplayFile(self, do_disconnect):
		if self.file_thread is not None:
			return
		self.file_thread = threading.Thread(target = self._createReplayFile, args=(do_disconnect, ))
		self.file_thread.setDaemon(True)
		self.flush()
		self.file_thread.start()

	def _createReplayFile(self, do_disconnect):
		try:
			self.flush()
			self.file_size = 0
			logger = copy.deepcopy(self.logger)
			logger.thread = 'File'

			if not os.path.isfile(utils.RecordingFileName):
				logger.warn('"{}" file not found, abort create replay file'.format(utils.RecordingFileName))
				return

			# Creating .mcpr zipfile based on timestamp
			logger.log('Time recorded: {}'.format(utils.convert_millis(utils.getMilliTime() - self.start_time)))
			file_name = datetime.datetime.today().strftime('PCRC_%Y_%m_%d_%H_%M_%S') + '.mcpr'
			logger.log('Creating "{}"'.format(file_name))
			self.chat('Creating .mcpr file')
			zipf = zipfile.ZipFile(file_name, 'w', zipfile.ZIP_DEFLATED)

			meta_data = {
				'singleplayer': False,
				'serverName': 'SECRET SERVER',
				'duration': utils.getMilliTime() - self.start_time,
				'date': utils.getMilliTime(),
				'mcversion': '1.14.4',
				'fileFormat': 'MCPR',
				'fileFormatVersion': '14',
				'protocol': 498,
				'generator': 'PCRC',
				'selfId': -1,
				'players': self.player_uuids
			}
			utils.addFile(zipf, 'markers.json', '[]')
			utils.addFile(zipf, 'mods.json', '{"requiredMods":[]}')
			utils.addFile(zipf, 'metaData.json', json.dumps(meta_data))
			utils.addFile(zipf, '{}.crc32'.format(utils.RecordingFileName), str(utils.crc32f(utils.RecordingFileName)))
			utils.addFile(zipf, utils.RecordingFileName)

			logger.log('Size of replay file "{}": {}MB'.format(file_name, utils.convert_file_size(os.path.getsize(file_name))))

			if self.config.upload_file:
				self.chat('Uploading .mcpr file')
				logger.log('Uploading "{}" to transfer.sh'.format(utils.RecordingFileName))
				try:
					ret, out = subprocess.getstatusoutput('curl --upload-file ./{0} https://transfer.sh/{0}'.format(file_name))
					url = out.splitlines()[-1]
					self.file_urls.append(url)
					msg = '"{}" url = {}'.format(file_name, url)
					self.chat(msg)
				except Exception as e:
					logger.error('Fail to upload "{}" to transfer.sh'.format(utils.RecordingFileName))
					logger.error(traceback.format_exc())

			if do_disconnect:
				logger.log('File operations finished, disconnect now')
				self.disconnect()
		finally:
			self.file_thread = None
			self.logger.log('Recorder stopped, ignore the BrokenPipeError error below XD')

	def canStart(self):
		return not self.isRecording() and self.file_thread is None

	def finishedStopping(self):
		return self.canStart()

	def start(self):
		if not self.canStart():
			return
		self.on_recording_start()
		# start the bot
		self.connect()
		# version check
		versionMap = {}
		for i in pycraft.SUPPORTED_MINECRAFT_VERSIONS.items():
			versionMap[i[1]] = i[0]
		protocol_version = self.connection.context.protocol_version
		self.logger.log('protocol = {}, mc version = {}'.format(protocol_version, versionMap[protocol_version]))
		if protocol_version != 498:
			self.logger.log('protocol version not support! should be 498 (MC version 1.14.4)')
			return False
		with open('protocol.json', 'r') as f:
			self.protocolMap = json.load(f)[str(protocol_version)]['Clientbound']
		return True

	# initializing stuffs
	def on_recording_start(self):
		self.recording = True
		open(utils.RecordingFileName, 'w').close()
		self.start_time = utils.getMilliTime()
		self.last_player_movement = self.start_time
		self.afk_time = 0
		self.last_t = 0
		self.last_no_player_movement = False
		self.player_ids = []
		self.player_uuids = []
		self.blocked_entity_ids = []
		self.file_buffer = bytearray()
		self.file_size = 0
		self.last_showinfo_time = 0
		self.packet_counter = 0
		self.last_showinfo_packetcounter = 0
		self.file_thread = None
		self.pos = None
		if 'Time Update' in utils.BAD_PACKETS:
			utils.BAD_PACKETS.remove('Time Update')

	def stop(self):
		if not self.isRecording():
			return
		self.logger.log('Stopping recorder')
		self.chat('Recorder Stopping')
		self.recording = False
		self.createReplayFile(True)

	def restart(self):
		self.logger.log('Restarting recorder')
		self.stop()
		self.logger.log('---------------------------------------')
		time.sleep(1)
		self.start()

	def _chat(self, text):
		for line in text.splitlines():
			packet = serverbound.play.ChatPacket()
			packet.message = line
			self.connection.write_packet(packet)
			self.logger.log('sent chat message "{}" to the server'.format(line))

	def chat(self, text):
		if self.isOnline():
			self._chat(text)
		else:
			self.logger.warn('Cannot chat when disconnected')

	def _respawn(self):
		packet = serverbound.play.ClientStatusPacket()
		packet.action_id = serverbound.play.ClientStatusPacket.RESPAWN
		self.connection.write_packet(packet)
		self.logger.log('sent respawn packet to the server')

	def respawn(self):
		if self.isOnline():
			self._respawn()
		else:
			self.logger.warn('Cannot respawn when disconnected')

	def _spectate(self, uuid):
		packet = serverbound.play.SpectatePacket()
		packet.target = uuid
		self.connection.write_packet(packet)
		self.logger.log('try spectate to entity(uuid = {})'.format(uuid))

	def spectate(self, uuid):
		if self.isOnline():
			self._spectate(uuid)
		else:
			self.logger.warn('Cannot send respawn when disconnected')

	def print_urls(self):
		if len(self.file_urls) == 0:
			self.chat('No url found')
		else:
			self.chat('There are {} uploaded files:'.format(len(self.file_urls)))
			for url in self.file_urls:
				self.chat(url)

	def processCommand(self, command, name, uuid):
		args = command.split(' ')  # !!PCRC <> <> <> <>
		if len(args) == 0 or args[0] != '!!PCRC' or name == self.config.username:
			return
		if len(args) == 1:
			self.chat(utils.CommandHelpMessage)
		elif len(args) == 2 and args[1] == 'status':
			self.chat('Time recorded/passed: {}/{}\nPacket Recorded: {}\nBuffer size: {}MB\nFile size: {}MB'.format(
				utils.convert_millis(self.timeRecorded()), utils.convert_millis(self.timePassed()),
				self.packet_counter, utils.convert_file_size(len(self.file_buffer)), utils.convert_file_size(self.file_size)
			))
		elif len(args) == 2 and args[1] in ['spectate', 'spec'] and name is not None and uuid is not None:
			self.chat('Spectating to {} (uuid = {})'.format(name, uuid))
			self.spectate(uuid)
		elif len(args) == 2 and args[1] == 'here':
			self.chat('!!here')
		elif len(args) == 2 and args[1] in ['where', 'location', 'loc', 'position', 'pos']:
			if self.pos is not None:
				self.chat('I\'m at {}'.format(utils.format_vector(self.pos)))
			else:
				self.chat('Idk where am I qwq')
		elif len(args) == 2 and args[1] in ['stop', 'exit']:
			self.stop()
		elif len(args) == 2 and args[1] in ['url', 'urls']:
			self.print_urls()
		else:
			self.chat('Unknown command! Type !!PCRC for help')
