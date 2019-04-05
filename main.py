from hbapi import HuobiServices as api
import logging as log
from enum import *
import math
import time
import json
import signal
import sys

log.basicConfig(
    level = log.INFO,
    format='[%(asctime)s] %(levelname)s [%(funcName)s %(filename)s:%(lineno)d] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    filename='./log/log.txt',
    filemode='a'
)
def get_currency(name):
    trade = 0.0
    frozen = 0.0
    try:
        resp = api.get_balance()
    except:
            log.error("get balance faild!")
            return -1,0,0
    if not resp or resp["status"] != "ok":
        log.error("get balance resp status not ok")
        return -1,0,0
    for v in resp["data"]["list"]:
        if v["currency"] == name and v["type"] == "trade":
            trade = float(v["balance"])
        if v["currency"] == name and v["type"] == "frozen":
            frozen = float(v["balance"])
    return frozen+trade,trade,frozen
def get_current_kline(name="htusdt",type="1min"):
    try:
        klines = api.get_kline(name,type,1)
    except:
        log.error("get kline failed")
        return -1.0
    if not klines or klines["status"] != "ok":
        log.error("get kline resp status not ok")
        return -1.0
    if len(klines["data"]) == 0:
        log.error("kline resp have no data")
        return -1.0
    return klines["data"][0]


def create_order(amout,buy,price = 0):
    """
    :arg amout: 只有在市场价买时才是usdt,其余均为ht
    :arg buy: 0 表示卖
              1 表示买
    :arg price: 默认0 以市场价交易
                非0 限价交易
    :return oid: -1 表示失败
    """
    log.info("create_order||amout=%d||buy=%d||price=%d"%(amout,buy,price))
    if buy:
        if price:
            type = "buy-limit"
        else:
            type = "buy-market"
    else:
        if price:
            type = "sell-limit"
        else:
            type = "sell-market"
    try:
        resp = api.send_order(amout,"htusdt",type,price)
    except:
        log.error("Create order failed!")
        return -1
    if not resp:
        log.error("Create order resp is null")
        return -1
    if resp["status"] != "ok":
        print(resp)
        log.warning("Create order resp status false||err-code=%s" % resp["err-code"])
        return -1
    return int(resp["data"])


#返回订单是否完全成交,成交usdt数量,成交ht数量
# -1 异常
#  0 未成交
#  1 成交
def get_order_info(oid):
    try :
        info  = api.order_info(oid)
    except:
        log.error("get order info error||oid=%d" % oid)
        return -1,-1
    if info["status"] != "ok":
        log.warning("get order info resp status not ok||oid=%d" % oid)
        return -1,-1
    if info["data"]["state"] == "filled":
        return 1,float(info["data"]["field-cash-amount"]),float(info["data"]["field-amount"])
    else:
        return 0,-1


def cancel_order(order_id):
    log.info("cancel_order||oid=%d" % order_id)
    if order_id == 0: return 0
    try:
        resp = api.cancel_order(order_id)
    except:
        log.error("Cancel order faild||oid=%d"%order_id)
        return -1
    if not resp:
        log.error("cancel order resp is null")
        return -1
    if resp["status"] != "ok":
        log.warning("cancel order resp status false||err-code=%s" % resp["err-code"])
        return -1
    return 0

def float_floor(x,n):
    return float(math.floor(x*10**n))/10**n

PROFIT_RATE = 0.05
STOP_LOSS_RATE = 0.9
PROFIT_SELL_RATE = 0.35
PROFIT_SELL_RATE_ADJ = 0.15

TICKET = 0.002

tq = []


class TradeStat(IntEnum):
    Finish = 0
    Normal = 1
    Profit = 2
    Loss = 3

class OneTrade:
    def __init__(self,buy_price,mount):
        self.his_max_price = 0
        self.buy_price = buy_price
        self.cur_price = 0

        self.init_usdt = mount

        self.loss_cnt = 1
        self.profit_cnt = 1

        self.stat = TradeStat.Normal

        self.loss_oid = 0

        self.total_ht = mount/buy_price*(1-TICKET)

        self.last_run_ret = 0

    #normal stat logic
    def normal(self):
        """
        :return 0 stat unchange
                1 stat changed!
               -1 execute error
        """
        if self.cur_price > self.init_usdt*(1+PROFIT_RATE)/(1-TICKET)/self.total_ht:
            oid = create_order(self.init_usdt,1)
            if oid == -1: return -1
            r = get_order_info(oid)
            price = self.cur_price
            if r[0] == 1: price = r[1]/r[2]
            self.stat = TradeStat.Profit
            tq.append(OneTrade(price,self.init_usdt))
            return 1
        if self.cur_price < self.buy_price*STOP_LOSS_RATE:
            oid = create_order(self.init_usdt,1)
            if oid == -1: return -1
            r = get_order_info(oid)
            if r[0] == 1:
                self.total_ht += r[2]
            else:
                self.total_ht += self.init_usdt/self.cur_price*(1-TICKET)
            self.stat = TradeStat.Loss
            return 1
        return 0

    #profit stat logic
    def profit(self):
        upline = self.buy_price*(1+(self.profit_cnt+1)*PROFIT_RATE)/(1-2*TICKET)

        sell_rate = PROFIT_SELL_RATE+(self.profit_cnt-1)*PROFIT_SELL_RATE_ADJ
        downline = self.buy_price*(1-sell_rate)+self.his_max_price*sell_rate

        if self.cur_price > upline:
            self.profit_cnt = self.profit_cnt+1
        elif self.cur_price < downline:
            oid = create_order(float_floor(self.total_ht,2),0)
            if oid == -1: return -1
            self.stat = TradeStat.Finish
            return 1
        return 0

    #loss stat logic
    def loss(self):
        #更新挂单状态
        if self.loss_oid :
            r = get_order_info(self.loss_oid)
            if r[0] == -1 : return -1
            if r[0] == 1:
                self.loss_oid = 0
                self.total_ht -= r[2]
                self.loss_cnt = 1

        #挂单已卖出 & 当前价格满足profit
        if self.loss_oid == 0 and self.cur_price > (self.init_usdt*(1+PROFIT_RATE)/(1-TICKET) + self.init_usdt)/self.total_ht :
            #从当前余额创建一个新交易
            trans_amout = self.init_usdt/self.cur_price
            tq.append(OneTrade(self.cur_price,self.init_usdt))

            #将本次交易状态置为profit
            self.total_ht -= trans_amout
            self.buy_price = self.init_usdt/(self.total_ht/(1-TICKET))
            self.stat = TradeStat.Profit
            return 1

        #检查当前价格,是否需要加投
        if self.cur_price < self.buy_price*(STOP_LOSS_RATE**(self.loss_cnt+1)):
            #cancel huang order
            if cancel_order(self.loss_oid) == -1: return -1
            self.loss_oid = 0

            #buy again
            oid = create_order(self.init_usdt*(2**self.loss_cnt),1)
            if oid == -1 : return -1
            r = get_order_info(oid)
            if r[0] == 1:
                self.total_ht += r[2]
            else:
                self.total_ht += self.init_usdt*(2**self.loss_cnt)/self.cur_price*(1-TICKET)

            #create a sell order
            total_usdt = self.init_usdt * 2**(1+self.loss_cnt)
            sell_price = (total_usdt+self.init_usdt*PROFIT_RATE)/(1-TICKET)/self.total_ht
            sell_amout = (total_usdt-2*self.init_usdt)/(1-TICKET)/sell_price
            sell_amout = float_floor(sell_amout,2)
            oid = create_order(sell_amout,0,sell_price)
            if oid == -1: return  -1

            #update flags
            self.loss_oid = oid
            self.loss_cnt += 1
        return 0


    def run(self):
        cur_kline = get_current_kline()
        if cur_kline == -1:
            return -1
        self.his_max_price = max(self.his_max_price,float(cur_kline["high"]),self.buy_price)
        self.cur_price = float(cur_kline["close"])
        if self.last_run_ret == 1:
            self.his_max_price = self.cur_price

        if self.stat == TradeStat.Profit:
            self.last_run_ret = self.profit()
        elif self.stat == TradeStat.Normal:
            self.last_run_ret = self.normal()
        elif self.stat == TradeStat.Loss:
            self.last_run_ret = self.loss()

        return self.last_run_ret

    def __str__(self):
        return "[cur=%.2f his_max=%.2f buy_price=%.2f ini_usdt=%.2f stat=%s p_cnt=%d l_cnt=%d total_ht=%.2f]" %\
               (self.cur_price,self.his_max_price,self.buy_price,self.init_usdt,self.stat,self.profit_cnt,self.loss_cnt,self.total_ht)

    @staticmethod
    def parse(json_obj):
        if isinstance(json_obj,str):
            json_obj = json.loads(json_obj)
        if json_obj["buy_price"] and json_obj["init_usdt"]:
            o = OneTrade(float(json_obj["buy_price"]),float(json_obj["init_usdt"]))
        else:
            return
        if json_obj.__contains__("stat"):
            o.stat = TradeStat(int(json_obj["stat"]))
        if json_obj.__contains__("loss_cnt"):
            o.loss_cnt = int(json_obj["loss_cnt"])
        if json_obj.__contains__("profit_cnt"):
            o.profit_cnt = int(json_obj["profit_cnt"])
        if json_obj.__contains__("loss_oid"):
            o.loss_oid = int(json_obj["loss_oid"])
        if json_obj.__contains__("total_ht"):
            o.total_ht = float(json_obj["total_ht"])
        if json_obj.__contains__("last_run_ret"):
            o.last_run_ret = int(json_obj["last_run_ret"])
        if json_obj.__contains__("his_max_price"):
            o.his_max_price = float(json_obj["his_max_price"])
        if json_obj.__contains__("cur_price"):
            o.cur_price = float(json_obj["cur_price"])
        return o

def load(dir = "./save/obj.txt"):
    with open(dir,"r") as f:
        lines = f.readlines()
    log.warning("========load trades=======")
    for i in range(0,len(lines)):
        tq.append(OneTrade.parse(lines[i]))
        log.info("load trade num=%d content=%s"%(i,tq[i]))

def save(dir = "./save/obj.txt"):
    with open(dir,"w") as f:
        for i in range(0,len(tq)):
            s = json.dumps(tq[i].__dict__)
            f.write(s+'\n')

def exit_hander(signum, frame):
    save()
    log.warning("program exit! signum=%d||frame=%s"%(signum,frame))

if __name__ == '__main__':
    signal.signal(signal.SIGINT, exit_hander)
    signal.signal(signal.SIGTERM, exit_hander)
    #signal.signal(signal.SIGTSTP, exit_hander)
    exit(0)
    load()
    try:
        i = 0
        while i < len(tq):
            tBegin = time.time()
            if tq[i].stat == TradeStat.Finish:
                del tq[i]
                continue
            ret = tq[i].run()
            tUsed = time.time() - tBegin
            log.info("run cycle time used %dms ret=%d trade=%s" % (math.floor(tUsed * 1000), ret, tq[i]))
            if len(tq) > 0: i = (i + 1)%len(tq)
            if ret == -1: time.sleep(1)
    except:
        exit_hander(-1,"Exception from main")
