"""
Экран "💎 Premium" и оплата звёздами Telegram (Telegram Stars).

Как это устроено физически (см. документацию Telegram Bot API про
Stars-платежи):
1. Бот показывает счёт (message.answer_invoice) с currency="XTR" и
   provider_token="" — для звёзд отдельный платёжный провайдер не нужен,
   Telegram обрабатывает списание сам, банковские данные бот не видит
   вообще никогда.
2. Человек нажимает "Оплатить" в самом Telegram — тот присылает боту
   pre_checkout_query, на который ОБЯЗАТЕЛЬНО нужно ответить в течение
   10 секунд (см. precheckout ниже), иначе Telegram сам отменит платёж.
3. После успешного списания Telegram присылает обычное сообщение с полем
   successful_payment — вот тут и продлеваем подписку в БД (см.
   database.requests.extend_premium).

Подписка = единственное поле User.premium_until в БД (см.
database/models.py) — партнёрский режим и любые другие будущие
Premium-фичи должны проверять её через database.requests.is_premium_active,
а не заводить собственный флаг "платный ли пользователь".
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import texts
from database.requests import extend_premium, get_or_create_user, get_partner, is_premium_active
from keyboards import PREMIUM_BUTTON_TEXT, premium_buy_keyboard

router = Router(name="subscription")

# Два периода оплаты — единственное место, где их нужно менять, если
# решим пересчитать тариф. Год посчитан с выгодой ~35% относительно
# 12 отдельных месячных платежей (12 × 249 = 2988⭐ vs 1990⭐).
PREMIUM_PRICE_STARS_MONTH = 249
PREMIUM_DURATION_DAYS_MONTH = 30
PREMIUM_PRICE_STARS_YEAR = 1990
PREMIUM_DURATION_DAYS_YEAR = 365

_PERIODS = {
    "month": (PREMIUM_PRICE_STARS_MONTH, PREMIUM_DURATION_DAYS_MONTH, "месяц"),
    "year": (PREMIUM_PRICE_STARS_YEAR, PREMIUM_DURATION_DAYS_YEAR, "год"),
}

# Свой идентификатор счёта на каждый период — сверяем его же в
# pre_checkout_query, просто чтобы не подтверждать вслепую платёж с
# чужим/неожиданным payload.
_INVOICE_PAYLOAD_PREFIX = "premium_"


async def show_premium_screen(message: Message, user_id: int, username: str | None = None) -> None:
    """
    message — куда отправить ответ (может быть чужим исходящим сообщением,
    если экран открыт инлайн-кнопкой с другого экрана, см.
    handlers/partner.py::partner_go_premium и handlers/profile.py::
    profile_premium) — а вот user_id/username ВСЕГДА берём отдельно, у
    настоящего кликнувшего человека, а не у message.from_user (для
    исходящего сообщения бота это был бы сам бот, а не человек).
    """
    user = await get_or_create_user(user_id=user_id, username=username)
    active = is_premium_active(user)

    if not active:
        # Своей подписки нет — но если пара уже есть и партнёр её оплатил,
        # у этого человека ДОЛЖЕН быть точно такой же полный доступ, без
        # намёка на "оформи свою" (см. database.requests.has_effective_premium
        # и texts.premium_inherited_status_text — экономика лишнего места в
        # подписке партнёра копеечная, а вот половинчатый доступ ломает саму
        # идею партнёрского режима).
        partner = await get_partner(user_id)
        if partner is not None and is_premium_active(partner):
            await message.answer(texts.premium_inherited_status_text(partner, partner.premium_until))
            return

    await message.answer(
        texts.premium_status_text(active, user.premium_until, PREMIUM_PRICE_STARS_MONTH, PREMIUM_PRICE_STARS_YEAR),
        reply_markup=premium_buy_keyboard(active).as_markup(),
    )


@router.message(F.text == PREMIUM_BUTTON_TEXT)
async def premium_button(message: Message) -> None:
    await show_premium_screen(message, message.from_user.id, message.from_user.username)


@router.message(Command("premium"))
async def cmd_premium(message: Message) -> None:
    await show_premium_screen(message, message.from_user.id, message.from_user.username)


@router.callback_query(F.data.startswith("premium_buy:"))
async def premium_buy(callback: CallbackQuery) -> None:
    period = callback.data.split(":", maxsplit=1)[1]
    if period not in _PERIODS:
        await callback.answer()
        return
    price, days, label_ru = _PERIODS[period]

    await callback.answer()
    await callback.message.answer_invoice(
        title="Thomas Koszaczy Premium",
        description=f"Партнёрский режим и все Premium-фичи на {days} дней.",
        payload=f"{_INVOICE_PAYLOAD_PREFIX}{period}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"Premium на {label_ru}", amount=price)],
    )


@router.pre_checkout_query()
async def precheckout(pre_checkout_query: PreCheckoutQuery) -> None:
    period = pre_checkout_query.invoice_payload.removeprefix(_INVOICE_PAYLOAD_PREFIX)
    if period not in _PERIODS:
        await pre_checkout_query.answer(
            ok=False, error_message="Что-то пошло не так со счётом — попробуй ещё раз через /premium."
        )
        return
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    period = message.successful_payment.invoice_payload.removeprefix(_INVOICE_PAYLOAD_PREFIX)
    _, days, _ = _PERIODS.get(period, (None, PREMIUM_DURATION_DAYS_MONTH, None))
    new_until = await extend_premium(user_id=message.from_user.id, days=days)
    await message.answer(texts.premium_purchased_text(new_until))
