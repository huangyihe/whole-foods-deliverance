import logging
from concurrent.futures import ThreadPoolExecutor

from config import SiteConfig, SlotLocators, INTERVAL
from deliverance.elements import (SlotElement, SlotElementMulti, PaymentRow,
                                  CartItem)
from deliverance.exceptions import Redirect, RouteRedirect
from deliverance.nav import Route, Waypoint, handle_redirect
from deliverance.nav_handlers import wait_for_auth
from deliverance.notify import alert, annoy, send_sms, send_telegram
from deliverance.utils import (wait_for_elements, wait_for_element, remove_qs,
                               dump_toml, conf_dependent, jitter)

log = logging.getLogger(__name__)


def clean_slotname(slot_or_str):
    if isinstance(slot_or_str, SlotElement):
        name = slot_or_str.full_name
    else:
        name = slot_or_str
    return name.lower().replace(' ', '')


@conf_dependent('slot_preference')
def get_prefs_from_conf(conf):
    log.info('Reading slot preferences from conf: {}'.format(conf))
    prefs = []
    for day, windows in conf.items():
        for window in windows:
            if window.lower() == 'any':
                if day.lower() == 'any':
                    log.info("'Any' day, 'Any' time specified. "
                             "Will look for first available slot")
                    return None
                prefs.append(day.lower())
            else:
                prefs.append(clean_slotname('::'.join([day, window])))
    return prefs


class NavCallables:
    @staticmethod
    @conf_dependent('options')
    def select_payment_method(browser, conf):
        pref_card = conf.get('preferred_card')
        if not pref_card:
            log.warning('Preferred card not provided')
        else:
            for element in browser.driver.find_elements(
                *browser.Locators.PAYMENT_ROW
            ):
                card_row = PaymentRow(element)
                if card_row.card_number == pref_card:
                    log.info("Selecting card ending in '{}'".format(pref_card))
                    card_row.select()
                    return
            log.warning("Card ending in '{}' not found.".format(pref_card))
        log.warning('Using default payment method')


class Browser:
    def __init__(self, driver, args):
        self.driver = driver
        self.args = args
        self.site_config = SiteConfig(args.service)
        self.Locators = self.site_config.Locators
        self.Patterns = self.site_config.Patterns
        self.slot_prefs = get_prefs_from_conf()
        self.executor = None
        self.slot_type = None
        self.build_routes()

    @property
    def current_url(self):
        return remove_qs(self.driver.current_url)

    def build_routes(self):
        self.routes = {}
        for route_name in self.site_config.routes:
            log.debug("Building route: '{}'".format(route_name))
            route_dict = self.site_config.routes[route_name]
            waypoints = []
            for waypoint_conf in route_dict['waypoints']:
                w = Waypoint(*waypoint_conf)
                if w.callable:
                    w.callable = getattr(NavCallables(), w.callable)
                waypoints.append(w)
            self.routes[route_name] = Route(
                route_dict['route_start'],
                *waypoints
            )

    def is_logged_in(self):
        if self.current_url == self.site_config.BASE_URL:
            try:
                text = wait_for_element(self.driver, self.Locators.LOGIN).text
                return self.Patterns.NOT_LOGGED_IN not in text
            except Exception:
                return False
        elif self.Patterns.AUTH_URL in self.current_url:
            return False
        else:
            # Lazily assume true if we are anywhere but BASE_URL / AUTH pattern
            return True

    def determine_slot_type(self):
        log.info('Determining delivery slot type')
        if self.driver.find_elements(*SlotLocators('multi').CONTAINER):
            log.warning('Detected multiple delivery option slot container')
            self.slot_type = 'multi'
            self.slot_cls = SlotElementMulti
        else:
            self.slot_type = 'single'
            self.slot_cls = SlotElement

    def get_slots(self, timeout=5):
        slot_route = self.routes.get('SLOT_SELECT')
        # Make sure we are on the slot select page. If not, nav there
        while not slot_route.waypoints[-1].check_current(self.current_url):
            try:
                handle_redirect(self)
            except Redirect:
                slot_route.navigate(self)
        # Wait for one of two possible slot container elements to be present
        wait_for_elements(self.driver, [SlotLocators().CONTAINER,
                                        SlotLocators('multi').CONTAINER])
        if not self.slot_type:
            self.determine_slot_type()
        log.info('Checking for available slots')
        slots = [
            self.slot_cls(e) for e in self.driver.find_elements(
                *SlotLocators(self.slot_type).SLOT
            )
        ]
        if slots:
            log.info('Found {} slots: \n{}'.format(
                len(slots), '\n'.join([s.full_name for s in slots])
            ))
        if slots and self.slot_prefs:
            log.info('Comparing available slots to prefs')
            preferred_slots = []
            for cmp in self.slot_prefs:
                for s in slots:
                    if cmp.startswith('any'):
                        if cmp.replace('any', '') in clean_slotname(s):
                            preferred_slots.append(s)
                    else:
                        if clean_slotname(s).startswith(cmp):
                            preferred_slots.append(s)
            if preferred_slots:
                log.info('Found {} preferred slots: {}'.format(
                    len(preferred_slots),
                    '\n'+'\n'.join([p.full_name for p in preferred_slots])
                ))
            return preferred_slots
        else:
            return slots

    def generate_message(self, slots):
        text = []
        for slot in slots:
            date = str(slot._date_element)
            if date not in text:
                text.extend(['', date])
            text.append(str(slot))
        if self.args.checkout:
            text.extend(
                ['\nWill attempt to checkout using slot:', slots[0].full_name]
            )
        if text:
            return '\n'.join(
                [self.site_config.service + " delivery slots found!", *text]
            )

    def save_removed_items(self):
        """Writes OOS items that have been removed from cart to a TOML file"""
        removed = []
        for item in self.driver.find_elements(*self.Locators.OOS_ITEM):
            if self.Patterns.OOS in item.text:
                removed.append({
                    'text': item.text.split(self.Patterns.OOS)[0],
                    'product_id': item.find_element_by_xpath(
                            ".//*[starts-with(@name, 'asin')]"
                        ).get_attribute('value')
                })
        if not removed:
            log.warning("Couldn't detect any removed items to save")
        else:
            dump_toml({'items': removed}, 'removed_items')

    def save_cart(self):
        jitter(.4)
        self.driver.get(self.site_config.BASE_URL
                        + self.site_config.cart_endpoint)
        cart = []
        for element in wait_for_elements(self.driver,
                                         self.Locators.CART_ITEMS):
            try:
                cart.append(CartItem(element).data)
            except Exception:
                log.warning('Failed to parse a cart item')
        if cart:
            dump_toml(
                {'cart_item': sorted(cart, key=lambda k: k['product_id'])},
                self.site_config.service.replace(' ', '') + '_cart'
            )

    def main_loop(self):
        wait_for_auth(self)
        if self.args.save_cart:
            try:
                self.save_cart()
            except Exception:
                log.error('Failed to save cart items')
        self.routes['SLOT_SELECT'].navigate(self)
        slots = self.get_slots()
        if slots:
            annoy()
            alert('Delivery slots available. What do you need me for?',
                  'Sosumi')
        else:
            self.executor = ThreadPoolExecutor()
        while not slots:
            log.info('No slots found :( waiting...')
            jitter(INTERVAL)
            self.driver.refresh()
            slots = self.get_slots()
            if slots:
                alert('Delivery slots found')
                message_body = self.generate_message(slots)
                self.executor.submit(send_sms, message_body)
                self.executor.submit(send_telegram, message_body)
                if not self.args.checkout:
                    break
                checked_out = False
                log.info('Attempting to select slot and checkout')
                while not checked_out:
                    try:
                        log.info('Selecting slot: ' + slots[0].full_name)
                        slots[0].select()
                        self.routes['CHECKOUT'].navigate(self)
                        checked_out = True
                        alert('Checkout complete', 'Hero')
                    except RouteRedirect:
                        log.warning(
                            'Checkout failed: Redirected to slot select'
                        )
                        slots = self.get_slots()
                        if not slots:
                            break
        if self.executor:
            self.executor.shutdown()
