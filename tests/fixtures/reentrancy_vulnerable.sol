// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * Reentrancy vulnerable contract.
 * Should be blocked (critical/high finding from Slither).
 */
contract ReentrancyVulnerable {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount, "Insufficient balance");
        // VULNERABILITY: External call before state update
        (bool success, ) = msg.sender.call{value: amount}("");
        require(success, "Transfer failed");
        // State update after external call - REENTRANCY
        balances[msg.sender] -= amount;
    }

    function getBalance() external view returns (uint256) {
        return address(this).balance;
    }
}