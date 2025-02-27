"""
    Support for W1000 energy portal
    
    Thanks to https://github.com/amargo/ for the login session ideas
    
"""
import logging

import aiohttp
import voluptuous as vol
import unicodedata

from homeassistant.core import callback, HomeAssistant
from homeassistant.helpers import discovery
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.const import (
    CONF_SCAN_INTERVAL,
)
import homeassistant.util.dt as dt_util

from bs4 import BeautifulSoup
import requests, yaml, re
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    async_import_statistics
)
import json
from os.path import exists

_LOGGER = logging.getLogger(__name__)

DOMAIN = "w1000-energy-monitor"

CONF_ENDPOINT = "url"
CONF_USERNAME = "login_user"
CONF_PASSWORD = "login_pass"
CONF_REPORTS = "reports"
CONF_INTERVAL = "scan_interval"

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Required(CONF_REPORTS): cv.string,
                vol.Optional(CONF_INTERVAL, default=60): cv.positive_int, # minutes
                vol.Optional(CONF_ENDPOINT, default="https://energia.eon-hungaria.hu/W1000"): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup(hass, config):
    scan_interval = config[DOMAIN][CONF_INTERVAL]

    monitor = w1k_Portal(hass, config[DOMAIN][CONF_USERNAME], config[DOMAIN][CONF_PASSWORD], config[DOMAIN][CONF_ENDPOINT], config[DOMAIN][CONF_REPORTS] )
    hass.data[DOMAIN] = monitor

    now = dt_util.utcnow()
    async_track_utc_time_change(
        hass,
        monitor.update,
        minute=range(now.minute % scan_interval, 60, scan_interval),
        second=now.second,
    )

    hass.async_create_task(
        discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    )

    return True



class w1k_API:

    def __init__(self, username, password, endpoint, reports):

        self.username = username
        self.password = password
        self.account_url = endpoint+"/Account/Login"
        self.profile_data_url = endpoint + "/ProfileData/ProfileData"
        self.lastlogin = None
        self.reports = [ x.strip() for x in reports.split(",") ]
        self.session = None
#        self.start_values = {'consumption': None, 'production': None}

    async def request_data(self, ssl=True):
        
        
        ret = {}
        for report in self.reports:
            _LOGGER.debug("reading report "+report)
            retitem = await self.read_reportname(report)
            ret[report] = retitem[0]
        
        return ret
        

    def mysession(self):
        if self.session:
            return self.session
    
        jar = aiohttp.CookieJar(unsafe=True)
        self.session = aiohttp.ClientSession(cookie_jar=jar)
        return self.session

    async def login(self, ssl=False):
        try:
            session = self.mysession()
            async with session.get(
                url=self.account_url, ssl=ssl
            ) as resp:
                content = (await resp.content.read()).decode("utf8")
                status = resp.status
            
            index_content = BeautifulSoup(content, "html.parser")
            dome = index_content.select('#pg-login input[name=__RequestVerificationToken]')[0]
            self.request_verification_token = dome.get("value")

            payload = {
                "__RequestVerificationToken": self.request_verification_token,
                "UserName": self.username,
                "Password": self.password,
            }
            
            header = {}
#            header["User-Agent"] =  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/106.0.0.0 Safari/537.36"
#            header["Referer"] = self.account_url

            async with session.post(
                url=self.account_url, data=payload, ssl=ssl
            ) as resp:
                content = (await resp.content.read()).decode("utf8")
                status = resp.status
            
            _LOGGER.debug("resp status http "+str(status) )
            match = re.findall( r'W1000.start\((.+)sessionTimeout', content.replace("\n", " ") )
            
            if not status == 200:
                _LOGGER.error("Login failed.")
                _LOGGER.debug("HTML page: "+content)
                _LOGGER.debug("Index page was: "+index_content)
                return False
                
            if len(match)==0:
                _LOGGER.error("could not find session data. invalid or locked account?")
                _LOGGER.debug("HTML page: "+content)
                return False
            
            respob = yaml.safe_load(match[0]+"}")
            self.currentUser = respob['currentUser']
            self.workareas = respob['workareas']
            self.lastlogin = datetime.utcnow()
            
            for workarea in self.workareas:
                for window in workarea['windows']:
                    _LOGGER.debug("found report "+window['name']+" in workarea "+workarea['name'] )

            return True
            
        except Exception as ex:
            availability = 'Offline'
            _LOGGER.error("exception at login")
            print(datetime.now(), "Error retrive data from {0}.".format(str(ex)))
            
    
    
    async def read_reportname(self, reportname: str):
        loginerror = False
        if not self.lastlogin or self.lastlogin + timedelta(minutes=10) < datetime.utcnow():
            loginerror = not await self.login()
        
        if loginerror:
            return [None]
        
        for workarea in self.workareas:
            for window in workarea['windows']:
                if window['name'] == reportname:
                    return await self.read_reportid( int(window['reportid']), reportname )
        
        _LOGGER.error("report "+reportname+" not found")
        return [None]
        

    async def read_reportid(self, reportid: int, reportname: str, ssl=True):
        now = datetime.utcnow()

        loginerror = False
        if not self.lastlogin or self.lastlogin + timedelta(hours=1) < datetime.utcnow():
            loginerror = not await self.login()
        
        if loginerror:
            return None
            
        since = (now + timedelta(days=-2)).strftime("%Y-%m-%dT23:59:59")
        until = (now + timedelta(days=0 )).strftime("%Y-%m-%dT%H:00:00")
        
        params = {
            "page": 1,"perPage": 96*3,
            "reportId": reportid,
            "since": since,
            "until": until,
            "_": (now - timedelta(hours=3)).strftime("%s557")
        }
        
        session = self.mysession()

        test = True
        
        file = 'w1000_'+reportname+'.json'
        if test and exists(file):
            jsonResponse = json.load(open(file))
            status = 200
        else:
            async with session.get(
                url=self.profile_data_url, data=params, ssl=ssl
            ) as resp:
                jsonResponse = await resp.json()
            status = resp.status

            if status == 200 and test:
                with open(file, 'w', encoding='utf-8') as f:
                    json.dump(jsonResponse, f, ensure_ascii=True, indent=4)

        if status == 200:
            lastvalue = None
            unit = None
            lasttime = None
            ret = []
            statistic_id = f'sensor.w1000_'+(''.join(ch for ch in unicodedata.normalize('NFKD', reportname) if not unicodedata.combining(ch)))
            
            hourly = {}
            # collect hourly sums and total
            for curve in jsonResponse:
                unit = curve['unit']
                hourly_sum = None
                _LOGGER.debug(f"curve: {curve['name']}")
                name = curve['name']
                for data in curve['data']:
                    if data['status'] > 0:
                        idx = data['time'][:13]
                        
                        if not idx in hourly:
                            hourly[idx] = { 'sum':0, 'state':0 }
                        
                        if name.endswith("A"):
                            hourly[idx]['sum'] += data['value']
                        
                        if '.8.' in name:
                            hourly[idx]['state'] = data['value']
                        
            state = 0
            statistics = []
            sumsum = 0
            
            for idx in hourly:
                # skip unknown state from the beginning
                if state + hourly[idx]['state'] == 0:
                    continue
                
                # create statistic entry
                timestamp = idx+":00:00+02:00"	#TODO: needs to calculate DST
                if hourly[idx]['state'] > 0:
                    state = hourly[idx]['state']
                else:
                    state += hourly[idx]['sum']
                
                sumsum += hourly[idx]['sum']
                
                if hourly[idx]['sum'] > 0:	# TODO: not sure if we can skip an hour when sum is zero. 
                    statistics.append(
                        StatisticData(
                            start = datetime.fromisoformat(timestamp).astimezone(),
                            state = round(state,3),
                            sum = sumsum
                        )
                    )

            ret.append( {'curve':curve['name'], 'last_value':state, 'unit':curve['unit'], 'last_time':timestamp} )
                
            metadata = StatisticMetaData(
                has_mean = False,
                has_sum = True,
                name = "w1000 "+reportname,
                source = 'recorder',
                statistic_id = statistic_id,
                unit_of_measurement = curve['unit'],
            )
#           _LOGGER.debug(metadata)
            _LOGGER.debug("import statistics: "+statistic_id+" count: "+str(len(statistics)))
                
            try:
                async_import_statistics(self._hass, metadata, statistics)
            except Exception as ex:
                _LOGGER.warn("exception at async_import_statistics '"+statistic_id+"': "+str(ex))
                    
        else:
            _LOGGER.warm("error reading repot: http "+str(status) )
            _LOGGER.debug( jsonResponse )

        return ret








class w1k_Portal(w1k_API):

    def __init__(self, hass, username, password, endpoint, reports):
        super().__init__(username, password, endpoint, reports)
        self._hass = hass
        self._data = {}
        self._update_listeners = []

    def get_data(self, name):
        return self._data.get(name)

    async def update(self, *args):
        json = await self.request_data()
        self._data = self._prepare_data(json)
        self._notify_listeners()

    def _prepare_data(self, json):
        out = {}
        for report in json:
            dta = json[report]
            if dta and 'curve' in dta:
                if '.8.' in dta['curve']:
                    state_class = 'total_increasing'
                else:
                    state_class = 'measurement'
                
                out[report] = { 'state': dta['last_value'], 'unit':dta['unit'], 'attributes':{
                    'curve':dta['curve'],
                    'generated':dta['last_time'],
                    'state_class': state_class,
                }}
                if dta['unit'].endswith('W') or dta['unit'].endswith('Var'):
                    out[report]['attributes']['device_class'] = 'power'
                if dta['unit'].endswith('Wh') or dta['unit'].endswith('Varh'):
                    out[report]['attributes']['device_class'] = 'energy'

        return out

    def add_update_listener(self, listener):
        self._update_listeners.append(listener)
        _LOGGER.debug(f"registered sensor: {listener.entity_id}")
        listener.update_callback()


    def _notify_listeners(self):
        for listener in self._update_listeners:
            listener.update_callback()
        _LOGGER.debug("Notifying all listeners")
