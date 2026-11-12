// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * Honeypot token with owner-controlled pause/blacklist - minimal implementation.
 * Should be blocked (high finding from custom detector).
 */
contract HoneypotToken {
    string public name;
    string public symbol;
    uint8 public decimals = 18;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    address public owner;
    bool public paused = false;
    mapping(address => bool) public isBlacklisted;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    event Paused(address account);
    event Unpaused(address account);
    event BlacklistUpdated(address account, bool status);

    constructor(string memory name_, string memory symbol_) {
        name = name_;
        symbol = symbol_;
        totalSupply = 1000000 * 10**18;
        balanceOf[msg.sender] = totalSupply;
        owner = msg.sender;
        emit Transfer(address(0), msg.sender, totalSupply);
    }

    function transfer(address to, uint256 value) external returns (bool) {
        require(!paused, "Transfers paused");
        require(!isBlacklisted[msg.sender], "Sender blacklisted");
        require(!isBlacklisted[to], "Recipient blacklisted");
        require(balanceOf[msg.sender] >= value, "Insufficient balance");
        balanceOf[msg.sender] -= value;
        balanceOf[to] += value;
        emit Transfer(msg.sender, to, value);
        return true;
    }

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        require(!paused, "Transfers paused");
        require(!isBlacklisted[from], "Sender blacklisted");
        require(!isBlacklisted[to], "Recipient blacklisted");
        require(balanceOf[from] >= value, "Insufficient balance");
        require(allowance[from][msg.sender] >= value, "Insufficient allowance");
        balanceOf[from] -= value;
        balanceOf[to] += value;
        allowance[from][msg.sender] -= value;
        emit Transfer(from, to, value);
        return true;
    }

    // Owner can pause ALL transfers
    function pause() external {
        require(msg.sender == owner, "Not owner");
        paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external {
        require(msg.sender == owner, "Not owner");
        paused = false;
        emit Unpaused(msg.sender);
    }

    // Owner can blacklist any address
    function setBlacklist(address account, bool status) external {
        require(msg.sender == owner, "Not owner");
        isBlacklisted[account] = status;
        emit BlacklistUpdated(account, status);
    }

    // Owner can mint unlimited tokens
    function mint(address to, uint256 amount) external {
        require(msg.sender == owner, "Not owner");
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }
}