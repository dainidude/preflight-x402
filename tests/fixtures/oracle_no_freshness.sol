// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * Contract using Chainlink oracle without freshness check - minimal implementation.
 * Should be review (medium finding from custom detector).
 */
interface AggregatorV3Interface {
    function latestRoundData()
        external
        view
        returns (
            uint80 roundId,
            int256 answer,
            uint256 startedAt,
            uint256 updatedAt,
            uint80 answeredInRound
        );
}

contract OracleNoFreshness {
    address public owner;
    AggregatorV3Interface public priceFeed;

    constructor(address _priceFeed) {
        owner = msg.sender;
        priceFeed = AggregatorV3Interface(_priceFeed);
    }

    // VULNERABILITY: No freshness check on latestRoundData
    function getPrice() external view returns (int256) {
        (, int256 price, , , ) = priceFeed.latestRoundData();
        return price;
    }

    function getPriceWithStaleCheck() external view returns (int256) {
        (uint80 roundId, int256 price, , uint256 updatedAt, ) = priceFeed.latestRoundData();
        require(updatedAt > 0, "Round not complete");
        require(block.timestamp - updatedAt <= 86400, "Price stale");
        return price;
    }

    function setPriceFeed(address _priceFeed) external {
        require(msg.sender == owner, "Not owner");
        priceFeed = AggregatorV3Interface(_priceFeed);
    }
}