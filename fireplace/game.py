import random
import time
from calendar import timegm
from itertools import chain
from hearthstone.enums import CardType, PlayState, PowSubType, State, Step, Zone
from .actions import Attack, BeginTurn, Death, EndTurn, EventListener, Play
from .card import THE_COIN
from .entity import Entity
from .managers import GameManager
from .utils import CardList
from .exceptions import GameOver


class BaseGame(Entity):
	type = CardType.GAME
	MAX_MINIONS_ON_FIELD = 7
	Manager = GameManager

	def __init__(self, players):
		self.data = None
		self.players = players
		super().__init__()
		for player in players:
			player.game = self
		self.state = State.INVALID
		self.step = None
		self.next_step = None
		self.turn = 0
		self.current_player = None
		self.minions_killed_this_turn = CardList()
		self.no_aura_refresh = False
		self.tick = 0
		self.active_aura_buffs = CardList()

	def __repr__(self):
		return "%s(players=%r)" % (self.__class__.__name__, self.players)

	def __iter__(self):
		return self.all_entities.__iter__()

	@property
	def game(self):
		return self

	@property
	def board(self):
		return CardList(chain(self.players[0].field, self.players[1].field))

	@property
	def decks(self):
		return CardList(chain(self.players[0].deck, self.players[1].deck))

	@property
	def hands(self):
		return CardList(chain(self.players[0].hand, self.players[1].hand))

	@property
	def characters(self):
		return CardList(chain(self.players[0].characters, self.players[1].characters))

	@property
	def all_entities(self):
		return CardList(chain(self.entities, self.hands, self.decks, self.graveyard))

	@property
	def graveyard(self):
		return CardList(chain(self.players[0].graveyard, self.players[1].graveyard))

	@property
	def entities(self):
		return CardList(chain([self], self.players[0].entities, self.players[1].entities))

	@property
	def live_entities(self):
		return CardList(chain(self.players[0].live_entities, self.players[1].live_entities))

	def filter(self, *args, **kwargs):
		return self.all_entities.filter(*args, **kwargs)

	def action_block(self, source, actions, type, index=-1, target=None, event_args=None):
		self.manager.action(self, type, source, index, target)
		ret = self.queue_actions(source, actions, event_args)
		self.manager.action_end(self, type, source)
		return ret

	def attack(self, source, target):
		type = PowSubType.ATTACK
		actions = [Attack(source, target)]
		return self.action_block(source, actions, type, target=target)

	def joust(self, source, challenger, defender, actions):
		type = PowSubType.JOUST
		return self.action_block(source, actions, type, event_args=[challenger, defender])

	def main_power(self, source, actions, target):
		type = PowSubType.POWER
		return self.action_block(source, actions, type, target=target)

	def play_card(self, card, target, index, choose):
		type = PowSubType.PLAY
		actions = [Play(card, target, index, choose)]
		return self.action_block(card, actions, type, index, target)

	def process_deaths(self):
		type = PowSubType.DEATHS
		actions = []
		for card in self.live_entities:
			if card.to_be_destroyed:
				card.zone = Zone.GRAVEYARD
				actions.append(Death(card))

		self.check_for_end_game()

		if actions:
			self.action_block(self, actions, type)

	def trigger(self, source, actions, event_args):
		"""
		Perform actions as a result of an event listener (TRIGGER)
		"""
		type = PowSubType.TRIGGER
		return self.action_block(source, actions, type, event_args=event_args)

	def cheat_action(self, source, actions):
		"""
		Perform actions as if a card had just triggered them
		"""
		return self.trigger(source, actions, event_args=None)

	def check_for_end_game(self):
		"""
		Check if one or more player is currently losing.
		End the game if they are.
		"""
		gameover = False
		for player in self.players:
			if player.playstate in (PlayState.CONCEDED, PlayState.DISCONNECTED):
				player.playstate = PlayState.LOSING
			if player.playstate == PlayState.LOSING:
				gameover = True

		if gameover:
			if self.players[0].playstate == self.players[1].playstate:
				for player in self.players:
					player.playstate = PlayState.TIED
			else:
				for player in self.players:
					if player.playstate == PlayState.LOSING:
						player.playstate = PlayState.LOST
					else:
						player.playstate = PlayState.WON
			self.state = State.COMPLETE
			raise GameOver("The game has ended.")

	def queue_actions(self, source, actions, event_args=None):
		"""
		Queue a list of \a actions for processing from \a source.
		Triggers an aura refresh afterwards.
		"""
		source.event_args = event_args
		ret = self.trigger_actions(source, actions)
		source.event_args = None
		self.refresh_auras()
		return ret

	def trigger_actions(self, source, actions):
		"""
		Performs a list of `actions` from `source`.
		This should seldom be called directly - use `queue_actions` instead.
		"""
		ret = []
		for action in actions:
			if isinstance(action, EventListener):
				# Queuing an EventListener registers it as a one-time event
				# This allows registering events from eg. play actions
				self.log("Registering event listener %r on %r", action, self)
				action.once = True
				# FIXME: Figure out a cleaner way to get the event listener target
				if source.type == CardType.SPELL:
					listener = source.controller
				else:
					listener = source
				listener._events.append(action)
			else:
				ret.append(action.trigger(source))
		return ret

	def pick_first_player(self):
		"""
		Picks and returns first player, second player
		In the default implementation, the first player is always
		"Player 0". Use CoinRules to decide it randomly.
		"""
		return self.players[0], self.players[1]

	def refresh_auras(self):
		if self.no_aura_refresh:
			return

		refresh_queue = []
		for entity in self.entities:
			for script in entity.update_scripts:
				refresh_queue.append((entity, script))

		for entity in self.hands:
			for script in entity.data.scripts.Hand.update:
				refresh_queue.append((entity, script))

		# Sort the refresh queue by refresh priority (used by eg. Lightspawn)
		refresh_queue.sort(key=lambda e: getattr(e[1], "priority", 50))
		for entity, action in refresh_queue:
			action.trigger(entity)

		buffs_to_destroy = []
		for buff in self.active_aura_buffs:
			if buff.tick < self.tick:
				buffs_to_destroy.append(buff)
		for buff in buffs_to_destroy:
			buff.remove()

		self.tick += 1

	def prepare(self):
		self.players[0].opponent = self.players[1]
		self.players[1].opponent = self.players[0]
		for player in self.players:
			player.zone = Zone.PLAY
			self.manager.new_entity(player)

		first, second = self.pick_first_player()
		self.player1 = first
		self.player1.first_player = True
		self.player2 = second
		self.player2.first_player = False

		for player in self.players:
			player.prepare_for_game()

	def start(self):
		self.log("Starting game %r", self)
		self.state = State.RUNNING
		self.step = Step.MAIN_BEGIN
		self.zone = Zone.PLAY
		self.prepare()
		self.manager.start_game()
		self.begin_turn(self.player1)

	def end_turn(self):
		return self.queue_actions(self, [EndTurn(self.current_player)])

	def _end_turn(self):
		self.log("%s ends turn %i", self.current_player, self.turn)
		self.manager.step(self.next_step, Step.MAIN_CLEANUP)
		self.current_player.temp_mana = 0
		self.end_turn_cleanup()

	def end_turn_cleanup(self):
		self.manager.step(self.next_step, Step.MAIN_NEXT)
		for character in self.current_player.characters.filter(frozen=True):
			if not character.num_attacks and not character.exhausted:
				self.log("Freeze fades from %r", character)
				character.frozen = False
		for buff in self.entities.filter(one_turn_effect=True):
			self.log("Ending One-Turn effect: %r", buff)
			buff.remove()
		self.begin_turn(self.current_player.opponent)

	def begin_turn(self, player):
		return self.queue_actions(self, [BeginTurn(player)])

	def _begin_turn(self, player):
		self.manager.step(self.next_step, Step.MAIN_READY)
		self.turn += 1
		self.log("%s begins turn %i", player, self.turn)
		self.current_player = player
		self.manager.step(self.next_step, Step.MAIN_START_TRIGGERS)
		self.manager.step(self.next_step, Step.MAIN_START)
		self.manager.step(self.next_step, Step.MAIN_ACTION)
		self.minions_killed_this_turn = CardList()

		for p in self.players:
			p.cards_drawn_this_turn = 0

		player.turn_start = timegm(time.gmtime())
		player.cards_played_this_turn = 0
		player.minions_played_this_turn = 0
		player.minions_killed_this_turn = 0
		player.combo = False
		player.max_mana += 1
		player.used_mana = 0
		player.overload_locked = player.overloaded
		player.overloaded = 0
		for entity in self.live_entities:
			if entity.type != CardType.PLAYER:
				entity.turns_in_play += 1

		if player.hero.power:
			player.hero.power.activations_this_turn = 0

		for character in self.characters:
			character.num_attacks = 0

		player.draw()
		self.manager.step(self.next_step, Step.MAIN_END)


class CoinRules:
	"""
	Randomly determines the starting player when the Game starts.
	The second player gets "The Coin" (GAME_005).
	"""
	def pick_first_player(self):
		winner = random.choice(self.players)
		self.log("Tossing the coin... %s wins!", winner)
		return winner, winner.opponent

	def start(self):
		super().start()
		self.log("%s gets The Coin (%s)", self.player2, THE_COIN)
		self.player2.give(THE_COIN)


class MulliganRules:
	"""
	Performs a Mulligan phase when the Game starts.
	Currently just a dummy phase.
	"""

	def start(self):
		from .actions import MulliganChoice

		self.next_step = Step.BEGIN_MULLIGAN
		super().start()

		self.log("Entering mulligan phase")
		self.step, self.next_step = self.next_step, Step.MAIN_READY

		for player in self.players:
			self.queue_actions(self, [MulliganChoice(player)])


class Game(MulliganRules, CoinRules, BaseGame):
	pass
