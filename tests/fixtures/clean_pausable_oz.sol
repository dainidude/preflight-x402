// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * Clean pausable token: standalone owner pause is acceptable.
 * Should be review, not block (pausable alone is medium severity).
 */
contract CleanPausableOZ {
    bool public paused = false;
    address public owner;
    mapping(address => uint256) public balanceOf;

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

    function transfer(address to, uint256 amount) external whenNotPaused returns (bool) {
        require(balanceOf[msg.sender] >= amount, "insufficient");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}
