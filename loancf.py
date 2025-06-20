# Copyright (c) 2024 Helen Zhang <zhangdhelen@gmail.com>
# Distributed under the BSD 3-Clause License

from dataclasses import dataclass, field
import pandas as pd
import numpy_financial as npf
import numpy as np
import torch
from scipy.optimize import brentq

# --- Utility Functions ---

cpr2smm = lambda cpr: 1-(1-cpr)**(1/12)

def safedivide(a, b):
    if np.isclose(b, 0, rtol=0, atol=3e-11):
        return 0
    else:
        return a / b

def calc(v): # for WAL calculations
    numerator = np.sum(np.maximum(0.0, v) * (np.arange(1, len(v) + 1) / 12.))
    denominator = np.sum(np.maximum(0.0, v))
    return safedivide(numerator, denominator) 

def shift_elements(arr, num, fill_value):
    result = np.empty_like(arr)
    if num > 0:
        result[:num] = fill_value
        result[num:] = arr[:-num]
    elif num < 0:
        result[num:] = fill_value
        result[:num] = arr[-num:]
    else:
        result[:] = arr
    return result       

def pad_zeros(vec, n, pad_value=0): # for lag
    if len(vec) < n:
        if pad_value == 'last':
            pad_val = vec[-1]
        else:
            pad_val = pad_value
        return np.concatenate([vec, np.full(n - len(vec), pad_val)])
    return vec


# --- Data Classes ---

@dataclass
class Scenario:
    smmV: np.ndarray # Single Monthly Mortality (prepayment vector)
    dqV: np.ndarray # Delinquency rate
    mdrV: np.ndarray # Monthly Default Rate
    sevV: np.ndarray # Severity
    recovery_lag: int=0
    refund_smm: np.ndarray=0 # treat as prepay
    refund_premium: float=1.0 # premium of discount
    aggMDR: float=0.0 # mdr value of percentage of B0
    aggMDR_timingV: np.ndarray=0 # mdr percentage monthly vector
    compIntHC: float=0.0 # Haircut
    servicing_fee: float=0.0
    is_advance: bool=False  #todo: adv | dq+default | dq=default at mon 1to4
    servicing_fee_method: str="avg"  # or "beg", toggle between avg and beginning bal servicing fee calculation


@dataclass
class Input:
    yieldValue: float=0.0
    fullpx: float=0.0


@dataclass
class Loan:
    wac: float  # Weighted Average Coupon (annual interest rate)
    wam: int    # Weighted Average Maturity (in months)
    pv: float   # Present Value (loan amount), pv is B0

    def getCashflow(self, scenario):
        wac = self.wac
        wam = self.wam
        pv = self.pv

        smmV = scenario.smmV
        dqV = scenario.dqV
        mdrV = scenario.mdrV
        sevV = scenario.sevV
        recovery_lag = scenario.recovery_lag
        period_with_lag = wam + recovery_lag # adjust number of rows to add lag
        refund_smm = scenario.refund_smm
        refund_premium = scenario.refund_premium
        dqMdrV = dqV + mdrV  # dqV is additional

        # Amortization
        rate = wac / 12
        X = -npf.pmt(rate, wam, pv) # Fixed monthly payment
        
        # Most vectors are wam length. survivorship, balances, and specifically noted balance (len: wam + 1). relating to servicing fee (len: wam + lag)
        monthsV = np.arange(1, wam + 1 + recovery_lag) # len: wam+lag
        balancesV = pv * (1 - (1 + rate) ** -(wam - np.arange(wam + 1))) / (1 - (1 + rate)**-wam) # len: wam+1
        interestsV = balancesV[:-1] * rate
        principalsV = X - interestsV
        paydownV = principalsV / balancesV[:-1]
        
        p_survV = np.cumprod(np.ones(wam) - smmV - refund_smm - mdrV)
        default_aggMDRV = pv*scenario.aggMDR * scenario.aggMDR_timingV
        dqPrin_aggMDRV = paydownV * default_aggMDRV
        scaled_default_aggMDRV = default_aggMDRV / ( balancesV[:-1] * p_survV )
        cum_scaled_default_aggMDRV = np.cumsum(scaled_default_aggMDRV)
        survivorshipV=np.insert(p_survV*(1-cum_scaled_default_aggMDRV),0,1) # wam+1

        actualBalanceV = survivorshipV * balancesV # len: wam+1
        b_balanceV= actualBalanceV[:-1] # beginning int bearing bal
        actualBalanceV = actualBalanceV[1:] # ending interest bearing balance
        balanceDiffV = b_balanceV - actualBalanceV       

        # Scheduled, prepayment, default, total principals, and deadbeat balance
        dqPrinV = np.zeros(wam) if scenario.is_advance else (
            survivorshipV[:-1] * principalsV * dqMdrV + dqPrin_aggMDRV )
        schedPrinV = survivorshipV[:-1] * principalsV - dqPrinV
        prepayPrinV = survivorshipV[:-1] * balancesV[1:] * smmV
        defaultV = b_balanceV * mdrV + default_aggMDRV
        # totalEndingBalV = actualBalanceV + dqPrinV 
        # totalBeginningBalV = b_balanceV.copy()
        # totalBeginningBalV[1:] = dqPrinV[:-1]

        # Losses, recoveries, and writedowns
        writedownV = defaultV #* sevV
        recoveryV = defaultV - writedownV # old calculation
        recoveryV = shift_elements(recoveryV, recovery_lag, 0) # old calculation
        writedownV = shift_elements(pad_zeros(writedownV, period_with_lag), recovery_lag, 0) # len: wam+lag, added writedownV shift
        recoveryV = writedownV * (1-pad_zeros(sevV, period_with_lag, pad_value='last')) # len: wam+lag, new calculation
        
        refundPrinV = survivorshipV[:-1] * balancesV[1:] * refund_smm
        totalPrinV = pad_zeros(schedPrinV, period_with_lag) + pad_zeros(prepayPrinV, period_with_lag) + recoveryV # len: wam+lag, padded for recovery lag
        compIntV = prepayPrinV * rate * scenario.compIntHC
        refundIntV = refundPrinV * rate
        prepayPrinV = survivorshipV[:-1] * balancesV[1:] * smmV + refundPrinV # added refundPrin to prepay calculation

        # Servicing Fee
        defaultBalV = np.maximum(0,np.cumsum(pad_zeros(defaultV, period_with_lag) - writedownV))
        b_totalBalV = pad_zeros(b_balanceV, period_with_lag) + np.insert(defaultBalV, 0, 0)[:-1]
        totalBalV = pad_zeros(actualBalanceV, period_with_lag) + defaultBalV

        servicingFee_rate = scenario.servicing_fee / 12
        servicingFee_begV = b_totalBalV * servicingFee_rate # len: wam+lag, uses beginning balance
        servicingFee_avgV = ((b_totalBalV + totalBalV) / 2) * servicingFee_rate # len: wam+lag
        
        if scenario.servicing_fee_method == "avg":
            servicingFeeV = servicingFee_avgV
        else:
            servicingFeeV = servicingFee_begV

        # Interest and Cash Flow
        actInterestV = rate*b_balanceV if scenario.is_advance else (
            rate*(b_balanceV*(1-dqMdrV) - default_aggMDRV) - compIntV)
        actInterestV -= refundIntV
        cfV = totalPrinV + pad_zeros(actInterestV, period_with_lag) # len: wam+lag, padded for recovery lag

        schedPrinV = pad_zeros(schedPrinV, period_with_lag)
        prepayPrinV = pad_zeros(prepayPrinV, period_with_lag)
        refundPrinV = pad_zeros(refundPrinV, period_with_lag)
        defaultV = pad_zeros(defaultV, period_with_lag)
        actInterestV = pad_zeros(actInterestV, period_with_lag)
        b_balanceV = pad_zeros(b_balanceV, period_with_lag)
        actualBalanceV = pad_zeros(actualBalanceV, period_with_lag)

        # Create scenario DataFrame
        df = pd.DataFrame({
            "Months": monthsV,
            "Prin": totalPrinV,
            "SchedPrin": schedPrinV,
            "Prepay Prin": prepayPrinV,
            "Refund Prin": refundPrinV,
            "Default": defaultV,
            "Writedown": writedownV,
            "Recovery": recoveryV,
            "Interest": actInterestV,
            "Servicing Fee": servicingFeeV,
            "Beginning Balance": b_balanceV,
            "Balance": actualBalanceV,
            "CFL": cfV,
        })
        
        return df
        
    def y2p(self, scenario, input): # yield to price
        yV = (1 + input.yieldValue/12)**np.arange(1, self.wam + 1 + scenario.recovery_lag) # len of months + recovery_lag

        cfV = self.getCashflow(scenario)["CFL"].values
        servicingFeeV = self.getCashflow(scenario)["Servicing Fee"].values
        refundPrinV = self.getCashflow(scenario).get("Refund Prin").values

        px = np.sum((cfV - servicingFeeV) / yV) / (self.pv - np.sum(refundPrinV / yV))
        return px

    def p2y(self, scenario, input): # price to yield
        price_target = input.fullpx

        def price_for_yield(y):
            class Helper:
                def __init__(self, yieldValue):
                    self.yieldValue = yieldValue
            input_obj = Helper(y)
            return self.y2p(scenario, input_obj) - price_target

        yield_solution = brentq(price_for_yield, 0.0001, 1.0)
        return yield_solution

class LoanAmort(torch.nn.Module): # takes in 2d tensor of n_loan, goes down nx3 each row is a loan
    def __init__(self, shape=(3, 3, 3)): 
        super(LoanAmort, self).__init__()
        self.tensor = torch.randn(shape)  # Initialize with random values
        self.shape = shape
    def forward(self): # takes in scenario tensor
        result = self.tensor + 1
        return result

if __name__ == '__main__':

    # Test 3D tensor
    model = LoanAmort()
    result = model()

    print(result)