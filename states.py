from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup


class AddCard(StatesGroup):
    waiting_for_text = State()


class Training(StatesGroup):
    flashcards = State()
    translate = State()
    listen = State()


async def leave_modes(state: FSMContext) -> None:
    """Exit any add/training mode and drop un-confirmed add-previews.

    Unlike `state.clear()` this deliberately PRESERVES incidental FSM data:
    - `vocab_voice_msg_id` — so `menu._remove_card_voice` can still delete the
      last vocab voice message (clearing it would orphan that message in chat);
    - `seq` — the monotonic preview-token counter, so a preview created before
      this exit can never share a token with one minted afterwards.
    `set_state(None)` drops the mode (so free text stops matching add/training);
    `pending={}` makes every still-visible preview's buttons go inert.
    """
    await state.set_state(None)
    await state.update_data(pending={})


async def end_session(state: FSMContext) -> None:
    """Finish a training session: exactly the former `state.clear()` (state AND all data
    dropped, incl. `vocab_voice_msg_id`) — except the monotonic `seq` survives, so an
    old still-visible preview button can never match a preview minted afterwards.
    """
    seq = (await state.get_data()).get("seq")
    await state.clear()
    if seq is not None:
        await state.update_data(seq=seq)
