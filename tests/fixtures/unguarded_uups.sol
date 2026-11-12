// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * UUPS upgradeable contract with unguarded initializer - minimal implementation.
 * Should be blocked or review (high finding from custom detector).
 */
contract UnguardedUUPS {
    address public owner;
    uint256 public value;
    address public implementation;
    bool public initialized = false;
    bool public initializing = false;

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    modifier initializer() {
        require(!initialized && !initializing, "Already initialized");
        initializing = true;
        _;
        initialized = true;
        initializing = false;
    }

    constructor() {
        initialized = true;
    }

    // VULNERABILITY: No access control on initialize
    function initialize(address _owner, uint256 _value) public initializer {
        owner = _owner;
        value = _value;
    }

    function setValue(uint256 _value) external onlyOwner {
        value = _value;
    }

    function upgradeTo(address newImplementation) external onlyOwner {
        implementation = newImplementation;
    }
}