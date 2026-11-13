// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * Honeypot token: owner can pause AND maintain a blacklist.
 * Should be blocked (pausable combined with owner-controlled blacklist is high severity).
 */
contract HoneypotPausableBlacklist {
    bool public paused = false;
    address public owner;
    mapping(address => uint256) public balanceOf;
    mapping(address => bool) public isBlacklisted;

    constructor() {
        owner = msg.sender;
        balanceOf[msg.sender] = 1_000_000 * 10 ** 18;
    }

    modifier whenNotPaused() {
        require(!paused, "paused");
        _;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    function pause() external onlyOwner {
        paused = true;
    }

    function unpause() external onlyOwner {
        paused = false;
    }

    function setBlacklist(address account, bool status) external onlyOwner {
        isBlacklisted[account] = status;
    }

    function transfer(address to, uint256 amount) external whenNotPaused returns (bool) {
        require(!isBlacklisted[to], "blacklisted recipient");
        require(!isBlacklisted[msg.sender], "blacklisted sender");
        require(balanceOf[msg.sender] >= amount, "insufficient");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}
