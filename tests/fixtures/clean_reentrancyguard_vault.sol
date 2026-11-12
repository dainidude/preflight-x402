// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * Clean vault: reentrancy-safe withdraw pattern.
 * Uses a manual mutex and checks-effects-interactions ordering.
 * Should pass all checks (looks_ok).
 */
contract CleanReentrancyGuardVault {
    mapping(address => uint256) public balances;
    bool private _locked;

    modifier nonReentrant() {
        require(!_locked, "ReentrancyGuard: reentrant call");
        _locked = true;
        _;
        _locked = false;
    }

    function deposit() public payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw(uint256 amount) external nonReentrant {
        uint256 bal = balances[msg.sender];
        require(bal >= amount, "Insufficient balance");

        // Effects before interaction.
        balances[msg.sender] = bal - amount;

        // Interaction.
        (bool success, ) = payable(msg.sender).call{value: amount}("");
        require(success, "Transfer failed");
    }

    receive() external payable {
        deposit();
    }
}
